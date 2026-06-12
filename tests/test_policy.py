"""Unit tests for release policy functionality.

Tests cover:
- Policy data model serialization/deserialization
- Default policy generation
- Policy loading and validation
- Policy evaluation against manifests
- Validation engine integration with policy
- Backward compatibility (old snapshots without policy)
"""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from typing import Any, Dict

from release_orchestrator.core.models import (
    ApprovalRecord,
    ApprovalStatus,
    Component,
    EnvironmentType,
    PackageArtifact,
    ReleaseManifest,
)
from release_orchestrator.core.policy import (
    EnvironmentPolicy,
    ReleasePolicy,
    default_policy,
)
from release_orchestrator.core.validator import ValidationEngine
from release_orchestrator.utils.policy_loader import (
    PolicyValidationError,
    evaluate_policy,
    load_policy,
    validate_policy_data,
    save_default_policy,
)
from release_orchestrator.utils.storage import (
    ExecutionSnapshot,
)


class TestEnvironmentPolicy(unittest.TestCase):
    """Tests for EnvironmentPolicy data class."""

    def test_default_values(self):
        ep = EnvironmentPolicy()
        self.assertFalse(ep.require_approval)
        self.assertFalse(ep.allow_version_downgrade)
        self.assertEqual(ep.skip_checksum_components, [])
        self.assertTrue(ep.dry_run_failure_blocks_export)

    def test_to_dict_and_back(self):
        ep = EnvironmentPolicy(
            require_approval=True,
            allow_version_downgrade=False,
            skip_checksum_components=["comp-a", "comp-b"],
            dry_run_failure_blocks_export=True,
        )
        d = ep.to_dict()
        self.assertEqual(d["require_approval"], True)
        self.assertEqual(d["allow_version_downgrade"], False)
        self.assertEqual(d["skip_checksum_components"], ["comp-a", "comp-b"])
        self.assertEqual(d["dry_run_failure_blocks_export"], True)

        ep2 = EnvironmentPolicy.from_dict(d)
        self.assertEqual(ep2.require_approval, True)
        self.assertEqual(ep2.allow_version_downgrade, False)
        self.assertEqual(ep2.skip_checksum_components, ["comp-a", "comp-b"])
        self.assertEqual(ep2.dry_run_failure_blocks_export, True)

    def test_from_dict_with_missing_fields(self):
        ep = EnvironmentPolicy.from_dict({})
        self.assertFalse(ep.require_approval)
        self.assertFalse(ep.allow_version_downgrade)
        self.assertEqual(ep.skip_checksum_components, [])
        self.assertTrue(ep.dry_run_failure_blocks_export)


class TestReleasePolicy(unittest.TestCase):
    """Tests for ReleasePolicy data class."""

    def test_default_policy_structure(self):
        policy = default_policy()
        self.assertEqual(policy.policy_version, "1.0")
        self.assertEqual(policy.default_environment, "production")
        self.assertIn("dev", policy.env_rules)
        self.assertIn("test", policy.env_rules)
        self.assertIn("staging", policy.env_rules)
        self.assertIn("production", policy.env_rules)

    def test_env_policy_lookup(self):
        policy = default_policy()
        prod_policy = policy.get_env_policy("production")
        self.assertTrue(prod_policy.require_approval)
        self.assertFalse(prod_policy.allow_version_downgrade)

        dev_policy = policy.get_env_policy("dev")
        self.assertFalse(dev_policy.require_approval)
        self.assertTrue(dev_policy.allow_version_downgrade)

    def test_env_policy_fallback_to_default(self):
        policy = default_policy()
        unknown_policy = policy.get_env_policy("unknown-env")
        prod_policy = policy.get_env_policy("production")
        self.assertEqual(
            unknown_policy.require_approval,
            prod_policy.require_approval,
        )

    def test_to_dict_and_from_dict(self):
        policy = default_policy()
        d = policy.to_dict()
        json_str = policy.to_json()
        data = json.loads(json_str)
        self.assertEqual(data["policy_version"], "1.0")
        self.assertIn("env_rules", data)
        self.assertIn("production", data["env_rules"])

        policy2 = ReleasePolicy.from_dict(d)
        self.assertEqual(policy2.policy_version, policy.policy_version)
        self.assertEqual(
            policy2.get_env_policy("production").require_approval,
            policy.get_env_policy("production").require_approval,
        )

    def test_from_json(self):
        policy = default_policy()
        json_str = policy.to_json()
        policy2 = ReleasePolicy.from_json(json_str)
        self.assertEqual(policy2.policy_version, policy.policy_version)
        self.assertEqual(len(policy2.env_rules), len(policy.env_rules))

    def test_list_known_environments(self):
        policy = default_policy()
        envs = policy.list_known_environments()
        self.assertIn("dev", envs)
        self.assertIn("production", envs)
        self.assertEqual(len(envs), 4)


class TestPolicyValidation(unittest.TestCase):
    """Tests for policy data validation."""

    def test_valid_policy_passes(self):
        policy = default_policy()
        errors = validate_policy_data(policy.to_dict())
        self.assertEqual(len(errors), 0)

    def test_missing_policy_version(self):
        data = default_policy().to_dict()
        del data["policy_version"]
        errors = validate_policy_data(data)
        self.assertTrue(any("policy_version" in e for e in errors))

    def test_missing_default_environment(self):
        data = default_policy().to_dict()
        del data["default_environment"]
        errors = validate_policy_data(data)
        self.assertTrue(any("default_environment" in e for e in errors))

    def test_missing_env_rules(self):
        data = default_policy().to_dict()
        del data["env_rules"]
        errors = validate_policy_data(data)
        self.assertTrue(any("env_rules" in e for e in errors))

    def test_env_rules_not_dict(self):
        data = default_policy().to_dict()
        data["env_rules"] = "not a dict"
        errors = validate_policy_data(data)
        self.assertTrue(any("env_rules" in e and "object" in e for e in errors))

    def test_boolean_field_wrong_type(self):
        data = default_policy().to_dict()
        data["env_rules"]["production"]["require_approval"] = "yes"
        errors = validate_policy_data(data)
        self.assertTrue(
            any("require_approval" in e and "boolean" in e for e in errors)
        )

    def test_skip_checksum_not_list(self):
        data = default_policy().to_dict()
        data["env_rules"]["production"]["skip_checksum_components"] = "comp1"
        errors = validate_policy_data(data)
        self.assertTrue(any("skip_checksum_components" in e for e in errors))

    def test_skip_checksum_item_not_string(self):
        data = default_policy().to_dict()
        data["env_rules"]["production"]["skip_checksum_components"] = [123, 456]
        errors = validate_policy_data(data)
        self.assertTrue(len(errors) > 0)


class _TestManifestBuilder:
    """Helper to build test manifests."""

    @staticmethod
    def make_component(
        name: str,
        version: str,
        env: EnvironmentType = EnvironmentType.PRODUCTION,
        deployed_version: str = "1.0.0",
        has_approval: bool = True,
        bad_checksum: bool = False,
    ) -> Component:
        content = f"pkg:{name}:{version}".encode()
        import hashlib

        checksum = hashlib.sha256(content).hexdigest()
        if bad_checksum:
            checksum = "0" * 64

        with tempfile.NamedTemporaryFile(
            delete=False, suffix=".pkg", prefix=f"test_{name}_"
        ) as f:
            f.write(content)
            artifact_path = f.name

        approvals = []
        if has_approval:
            approvals = [
                ApprovalRecord(
                    approver="test@example.com",
                    status=ApprovalStatus.APPROVED,
                    timestamp="2025-01-01T00:00:00Z",
                )
            ]

        return Component(
            name=name,
            version=version,
            environment=env,
            artifact=PackageArtifact(
                path=artifact_path,
                checksum=checksum,
                checksum_algorithm="sha256",
            ),
            approvals=approvals,
            deployed_version=deployed_version,
        )

    @staticmethod
    def make_manifest(
        components: list, target_env: EnvironmentType = EnvironmentType.PRODUCTION
    ) -> ReleaseManifest:
        return ReleaseManifest(
            manifest_version="1.0",
            release_id="REL-TEST-001",
            title="Test Release",
            target_environment=target_env,
            components=components,
        )


class TestPolicyValidationIntegration(unittest.TestCase):
    """Tests for policy integration with ValidationEngine."""

    def setUp(self):
        self.builder = _TestManifestBuilder()

    def tearDown(self):
        """Clean up temp artifact files."""
        import glob

        for f in glob.glob(os.path.join(tempfile.gettempdir(), "test_*_*.pkg")):
            try:
                os.unlink(f)
            except OSError:
                pass

    def test_approval_required_by_policy(self):
        comp = self.builder.make_component(
            "svc-a", "2.0.0", has_approval=False
        )
        manifest = self.builder.make_manifest([comp])

        policy = ReleasePolicy(
            env_rules={
                "production": EnvironmentPolicy(require_approval=True)
            }
        )
        engine = ValidationEngine(manifest, policy=policy)
        result = engine.validate(verify_checksums=False)

        has_approval_error = any(
            i.issue_code == "APPROVAL_MISSING" and i.severity.value == "error"
            for i in result.issues
        )
        self.assertTrue(has_approval_error)

    def test_approval_not_required_by_policy(self):
        comp = self.builder.make_component(
            "svc-a", "2.0.0", has_approval=False
        )
        manifest = self.builder.make_manifest([comp])

        policy = ReleasePolicy(
            env_rules={
                "production": EnvironmentPolicy(require_approval=False)
            }
        )
        engine = ValidationEngine(manifest, policy=policy)
        result = engine.validate(verify_checksums=False)

        has_approval_error = any(
            i.issue_code == "APPROVAL_MISSING" and i.severity.value == "error"
            for i in result.issues
        )
        self.assertFalse(has_approval_error)

    def test_version_downgrade_blocked_by_policy(self):
        comp = self.builder.make_component(
            "svc-b", "0.9.0", deployed_version="1.0.0"
        )
        manifest = self.builder.make_manifest([comp])

        policy = ReleasePolicy(
            env_rules={
                "production": EnvironmentPolicy(allow_version_downgrade=False)
            }
        )
        engine = ValidationEngine(manifest, policy=policy)
        result = engine.validate(verify_checksums=False)

        has_downgrade_error = any(
            i.issue_code == "VERSION_DOWNGRADE" and i.severity.value == "error"
            for i in result.issues
        )
        self.assertTrue(has_downgrade_error)

    def test_version_downgrade_allowed_by_policy(self):
        comp = self.builder.make_component(
            "svc-b", "0.9.0", deployed_version="1.0.0"
        )
        manifest = self.builder.make_manifest([comp])

        policy = ReleasePolicy(
            env_rules={
                "production": EnvironmentPolicy(allow_version_downgrade=True)
            }
        )
        engine = ValidationEngine(manifest, policy=policy)
        result = engine.validate(verify_checksums=False)

        has_downgrade_error = any(
            i.issue_code == "VERSION_DOWNGRADE" and i.severity.value == "error"
            for i in result.issues
        )
        self.assertFalse(has_downgrade_error)
        has_downgrade_info = any(
            i.issue_code == "VERSION_DOWNGRADE_ALLOWED"
            for i in result.issues
        )
        self.assertTrue(has_downgrade_info)

    def test_checksum_skipped_by_policy(self):
        comp = self.builder.make_component(
            "svc-c", "1.0.0", bad_checksum=True
        )
        manifest = self.builder.make_manifest([comp])

        policy = ReleasePolicy(
            env_rules={
                "production": EnvironmentPolicy(
                    skip_checksum_components=["svc-c"]
                )
            }
        )
        engine = ValidationEngine(manifest, policy=policy)
        result = engine.validate(verify_checksums=True)

        has_checksum_mismatch = any(
            i.issue_code == "CHECKSUM_MISMATCH" for i in result.issues
        )
        self.assertFalse(has_checksum_mismatch)
        has_skip_info = any(
            i.issue_code == "CHECKSUM_SKIPPED_BY_POLICY" for i in result.issues
        )
        self.assertTrue(has_skip_info)

    def test_unknown_component_in_skip_list_warning(self):
        comp = self.builder.make_component("svc-d", "1.0.0")
        manifest = self.builder.make_manifest([comp])

        policy = ReleasePolicy(
            env_rules={
                "production": EnvironmentPolicy(
                    skip_checksum_components=["nonexistent-comp"]
                )
            }
        )
        engine = ValidationEngine(manifest, policy=policy)
        result = engine.validate(verify_checksums=False)

        has_warning = any(
            i.issue_code == "POLICY_UNKNOWN_COMPONENT"
            for i in result.issues
        )
        self.assertTrue(has_warning)

    def test_default_policy_applies_to_production(self):
        comp = self.builder.make_component(
            "svc-e", "2.0.0", has_approval=False
        )
        manifest = self.builder.make_manifest(
            [comp], target_env=EnvironmentType.PRODUCTION
        )

        engine = ValidationEngine(manifest)
        result = engine.validate(verify_checksums=False)

        has_approval_error = any(
            i.issue_code == "APPROVAL_MISSING" and i.severity.value == "error"
            for i in result.issues
        )
        self.assertTrue(has_approval_error)


class TestPolicyLoader(unittest.TestCase):
    """Tests for policy loading from files."""

    def test_load_policy_from_file(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            policy_path = Path(tmpdir) / "policy.json"
            policy = default_policy()
            policy_path.write_text(policy.to_json(), encoding="utf-8")

            loaded = load_policy(str(policy_path))
            self.assertEqual(loaded.policy_version, policy.policy_version)
            self.assertEqual(len(loaded.env_rules), len(policy.env_rules))

    def test_load_policy_file_not_found_explicit(self):
        with self.assertRaises(FileNotFoundError):
            load_policy("/nonexistent/path/policy.json")

    def test_load_policy_invalid_json(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            policy_path = Path(tmpdir) / "bad.json"
            policy_path.write_text("not json{", encoding="utf-8")

            with self.assertRaises(PolicyValidationError):
                load_policy(str(policy_path))

    def test_load_policy_default_fallback(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            loaded = load_policy(work_dir=tmpdir)
            self.assertEqual(loaded.policy_version, "1.0")
            self.assertIn("production", loaded.env_rules)

    def test_save_default_policy(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "default_policy.json"
            result = save_default_policy(str(path))
            self.assertTrue(result.exists())
            self.assertTrue(result.stat().st_size > 0)

            data = json.loads(result.read_text(encoding="utf-8"))
            self.assertIn("policy_version", data)
            self.assertIn("env_rules", data)


class TestPolicyEvaluation(unittest.TestCase):
    """Tests for policy evaluation summary generation."""

    def setUp(self):
        self.builder = _TestManifestBuilder()

    def tearDown(self):
        import glob
        for f in glob.glob(os.path.join(tempfile.gettempdir(), "test_*_*.pkg")):
            try:
                os.unlink(f)
            except OSError:
                pass

    def test_evaluate_policy_basic(self):
        comp = self.builder.make_component("svc-a", "1.0.0")
        manifest = self.builder.make_manifest([comp])
        policy = default_policy()

        summary = evaluate_policy(policy, manifest)
        self.assertEqual(summary["target_environment"], "production")
        self.assertIn("rules_applied", summary)
        self.assertIn("component_impact", summary)
        self.assertIn("warnings", summary)

    def test_evaluate_policy_rules_applied(self):
        comp = self.builder.make_component("svc-a", "1.0.0")
        manifest = self.builder.make_manifest(
            [comp], target_env=EnvironmentType.PRODUCTION
        )
        policy = default_policy()

        summary = evaluate_policy(policy, manifest)
        rules = summary["rules_applied"]
        self.assertIn("require_approval", rules)
        self.assertIn("block_version_downgrade", rules)
        self.assertIn("dry_run_failure_blocks_export", rules)

    def test_evaluate_policy_component_impact(self):
        comp = self.builder.make_component("svc-a", "1.0.0")
        manifest = self.builder.make_manifest(
            [comp], target_env=EnvironmentType.PRODUCTION
        )
        policy = ReleasePolicy(
            env_rules={
                "production": EnvironmentPolicy(
                    require_approval=True,
                    skip_checksum_components=["svc-a"],
                )
            }
        )

        summary = evaluate_policy(policy, manifest)
        impact = summary["component_impact"]
        self.assertIn("svc-a", impact)
        self.assertIn("approval_required", impact["svc-a"])
        self.assertIn("checksum_skipped", impact["svc-a"])

    def test_evaluate_policy_unknown_component_warning(self):
        comp = self.builder.make_component("svc-a", "1.0.0")
        manifest = self.builder.make_manifest([comp])
        policy = ReleasePolicy(
            env_rules={
                "production": EnvironmentPolicy(
                    skip_checksum_components=["ghost-comp"]
                )
            }
        )

        summary = evaluate_policy(policy, manifest)
        self.assertTrue(len(summary["warnings"]) > 0)
        self.assertTrue(any("ghost-comp" in w for w in summary["warnings"]))

    def test_evaluate_policy_dev_env(self):
        comp = self.builder.make_component(
            "svc-a", "1.0.0", env=EnvironmentType.DEV
        )
        manifest = self.builder.make_manifest(
            [comp], target_env=EnvironmentType.DEV
        )
        policy = default_policy()

        summary = evaluate_policy(policy, manifest)
        self.assertEqual(summary["target_environment"], "dev")
        rules = summary["rules_applied"]
        self.assertNotIn("require_approval", rules)


class TestBackwardCompatibility(unittest.TestCase):
    """Tests for backward compatibility with old history entries."""

    def test_execution_snapshot_without_policy_fields(self):
        old_snapshot_data = {
            "run_id": "RUN-OLD-001",
            "command": "validate",
            "started_at": "2025-01-01T00:00:00Z",
            "finished_at": "2025-01-01T00:01:00Z",
            "exit_code": 0,
            "config_snapshot": {"args": {}},
            "manifest_snapshot": {"release_id": "REL-OLD"},
            "validation_result": {
                "timestamp": "2025-01-01T00:00:30Z",
                "passed": True,
                "issues": [],
            },
            "release_plan": None,
            "rollback_plan": None,
            "dry_run_result": None,
            "logs": [],
        }

        snap = ExecutionSnapshot.from_dict(old_snapshot_data)
        self.assertEqual(snap.run_id, "RUN-OLD-001")
        self.assertIsNone(snap.policy_snapshot)
        self.assertIsNone(snap.policy_summary)
        self.assertEqual(snap.exit_code, 0)

    def test_execution_snapshot_with_policy_fields(self):
        new_snapshot_data = {
            "run_id": "RUN-NEW-001",
            "command": "validate",
            "started_at": "2025-01-01T00:00:00Z",
            "finished_at": "2025-01-01T00:01:00Z",
            "exit_code": 0,
            "config_snapshot": {"args": {}},
            "manifest_snapshot": {"release_id": "REL-NEW"},
            "policy_snapshot": {"policy_version": "1.0", "env_rules": {}},
            "policy_summary": {
                "target_environment": "production",
                "rules_applied": [],
            },
            "validation_result": {
                "timestamp": "2025-01-01T00:00:30Z",
                "passed": True,
                "issues": [],
            },
            "release_plan": None,
            "rollback_plan": None,
            "dry_run_result": None,
            "logs": [],
        }

        snap = ExecutionSnapshot.from_dict(new_snapshot_data)
        self.assertEqual(snap.run_id, "RUN-NEW-001")
        self.assertIsNotNone(snap.policy_snapshot)
        self.assertEqual(snap.policy_snapshot["policy_version"], "1.0")
        self.assertIsNotNone(snap.policy_summary)
        self.assertEqual(snap.policy_summary["target_environment"], "production")


if __name__ == "__main__":
    unittest.main()
