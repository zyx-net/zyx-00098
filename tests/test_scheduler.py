"""Unit tests for the scheduling engine."""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path

from release_orchestrator.core.models import (
    ApprovalRecord,
    ApprovalStatus,
    Component,
    ComponentStatus,
    Dependency,
    EnvironmentType,
    FreezePeriod,
    PackageArtifact,
    ReleaseManifest,
    ReleaseWindow,
    ScheduleResult,
    Severity,
    Wave,
)
from release_orchestrator.core.policy import EnvironmentPolicy, ReleasePolicy, default_policy
from release_orchestrator.core.scheduler import (
    SchedulingEngine,
    load_waves_from_json,
    load_windows_from_csv,
    load_windows_from_json,
)
from release_orchestrator.utils.exit_codes import (
    EXIT_APPROVAL_MISSING,
    EXIT_CONFIG_ERROR,
    EXIT_OK,
    EXIT_SCHEDULE_ERROR,
    EXIT_WINDOW_LOCKED,
)


class _TestManifestBuilder:
    """Helper to build test manifests and components for scheduler tests."""

    @staticmethod
    def make_artifact(name: str, version: str, bad_checksum: bool = False) -> PackageArtifact:
        content = f"pkg:{name}:{version}".encode()
        checksum = hashlib.sha256(content).hexdigest()
        if bad_checksum:
            checksum = "0" * 64
        tmpdir = tempfile.gettempdir()
        artifact_path = os.path.join(tmpdir, f"test_{name}_{version}.pkg")
        with open(artifact_path, "wb") as f:
            f.write(content)
        return PackageArtifact(path=artifact_path, checksum=checksum, checksum_algorithm="sha256")

    @staticmethod
    def make_component(
        name: str,
        version: str = "1.0.0",
        env: EnvironmentType = EnvironmentType.PRODUCTION,
        deps=None,
        has_approval: bool = True,
        approver: str = "release-manager@corp.com",
    ) -> Component:
        approvals = []
        if has_approval:
            approvals = [
                ApprovalRecord(
                    approver=approver,
                    status=ApprovalStatus.APPROVED,
                    timestamp="2026-06-01T00:00:00Z",
                )
            ]
        return Component(
            name=name,
            version=version,
            environment=env,
            artifact=_TestManifestBuilder.make_artifact(name, version),
            dependencies=deps or [],
            approvals=approvals,
        )

    @staticmethod
    def make_manifest(components: list, release_id: str = "RL-TEST") -> ReleaseManifest:
        return ReleaseManifest(
            manifest_version="1.0",
            release_id=release_id,
            title="Test Release",
            target_environment=EnvironmentType.PRODUCTION,
            description="For scheduler tests",
            components=components,
        )


def _make_window(
    window_id,
    name,
    start="2026-06-15T09:00:00Z",
    end="2026-06-15T17:00:00Z",
    capacity=None,
    envs=None,
    roles=None,
    freezes=None,
    locked=False,
):
    return ReleaseWindow(
        window_id=window_id,
        name=name,
        start_time=start,
        end_time=end,
        capacity_max=capacity,
        allowed_environments=envs or [],
        required_approval_roles=roles or [],
        freeze_periods=freezes or [],
        locked=locked,
    )


class TestReleaseWindowModel(unittest.TestCase):
    def test_window_round_trip(self):
        fp = FreezePeriod(name="holiday", start="2026-01-01T00:00:00Z", end="2026-01-02T00:00:00Z", reason="New Year")
        w = _make_window("WIN-1", "Test Window", capacity=3, envs=["production"], roles=["rm@corp.com"], freezes=[fp])
        d = w.to_dict()
        w2 = ReleaseWindow.from_dict(d)
        self.assertEqual(w2.window_id, "WIN-1")
        self.assertEqual(w2.capacity_max, 3)
        self.assertEqual(w2.allowed_environments, ["production"])
        self.assertEqual(w2.required_approval_roles, ["rm@corp.com"])
        self.assertEqual(len(w2.freeze_periods), 1)
        self.assertEqual(w2.freeze_periods[0].name, "holiday")

    def test_wave_round_trip(self):
        wave = Wave(wave_id="W1", name="Infra", order=1, description="core")
        d = wave.to_dict()
        w2 = Wave.from_dict(d)
        self.assertEqual(w2.wave_id, "W1")
        self.assertEqual(w2.order, 1)


class TestWindowLoading(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_load_json_windows(self):
        data = {
            "windows": [
                {
                    "window_id": "WIN-A",
                    "name": "A",
                    "start_time": "2026-06-01T00:00:00Z",
                    "end_time": "2026-06-01T12:00:00Z",
                }
            ]
        }
        p = self.tmp / "w.json"
        p.write_text(json.dumps(data), encoding="utf-8")
        windows = load_windows_from_json(str(p))
        self.assertEqual(len(windows), 1)
        self.assertEqual(windows[0].window_id, "WIN-A")

    def test_load_json_windows_generates_id(self):
        data = [{"name": "Auto", "start_time": "2026-06-01T00:00:00Z", "end_time": "2026-06-01T12:00:00Z"}]
        p = self.tmp / "w2.json"
        p.write_text(json.dumps(data), encoding="utf-8")
        windows = load_windows_from_json(str(p))
        self.assertEqual(len(windows), 1)
        self.assertTrue(windows[0].window_id.startswith("WIN-"))

    def test_load_csv_windows(self):
        csv_text = (
            "window_id,name,start_time,end_time,timezone,capacity_max,allowed_environments\n"
            "WIN-C,C,2026-06-01T00:00:00Z,2026-06-01T12:00:00Z,UTC,5,staging,production\n"
        )
        p = self.tmp / "w.csv"
        p.write_text(csv_text, encoding="utf-8")
        windows = load_windows_from_csv(str(p))
        self.assertEqual(len(windows), 1)
        self.assertEqual(windows[0].window_id, "WIN-C")
        self.assertEqual(windows[0].capacity_max, 5)
        self.assertIn("staging", windows[0].allowed_environments)

    def test_load_csv_windows_with_freeze(self):
        csv_text = (
            "window_id,name,start_time,end_time,freeze_periods\n"
            "WIN-D,D,2026-06-01T00:00:00Z,2026-06-30T23:59:59Z,"
            "Freeze1|2026-06-15T00:00:00Z|2026-06-16T00:00:00Z|reason\n"
        )
        p = self.tmp / "wfreeze.csv"
        p.write_text(csv_text, encoding="utf-8")
        windows = load_windows_from_csv(str(p))
        self.assertEqual(len(windows[0].freeze_periods), 1)
        self.assertEqual(windows[0].freeze_periods[0].name, "Freeze1")
        self.assertEqual(windows[0].freeze_periods[0].reason, "reason")

    def test_load_missing_file_raises(self):
        with self.assertRaises(FileNotFoundError):
            load_windows_from_json(str(self.tmp / "nonexistent.json"))
        with self.assertRaises(FileNotFoundError):
            load_windows_from_csv(str(self.tmp / "nonexistent.csv"))

    def test_load_waves_from_json(self):
        data = {"waves": [{"wave_id": "W1", "name": "Infra", "order": 1}]}
        p = self.tmp / "waves.json"
        p.write_text(json.dumps(data), encoding="utf-8")
        waves = load_waves_from_json(str(p))
        self.assertEqual(len(waves), 1)
        self.assertEqual(waves[0].name, "Infra")


class TestSchedulingEngine(unittest.TestCase):
    def setUp(self):
        self.builder = _TestManifestBuilder()

    def tearDown(self):
        import glob
        for f in glob.glob(os.path.join(tempfile.gettempdir(), "test_*_*.pkg")):
            try:
                os.unlink(f)
            except OSError:
                pass

    def test_basic_scheduling(self):
        c = self.builder.make_component("svc-a", env=EnvironmentType.STAGING)
        manifest = self.builder.make_manifest([c])
        w = _make_window("WIN-1", "Staging Window", envs=["staging", "production"])
        engine = SchedulingEngine(manifest, [w])
        result = engine.schedule()
        self.assertEqual(result.total_scheduled, 1)
        self.assertEqual(result.entries[0].window_id, "WIN-1")
        self.assertEqual(result.entries[0].component_name, "svc-a")

    def test_empty_windows_returns_error(self):
        c = self.builder.make_component("svc-a")
        manifest = self.builder.make_manifest([c])
        engine = SchedulingEngine(manifest, [])
        result = engine.schedule()
        self.assertEqual(result.total_scheduled, 0)
        self.assertTrue(any(i.issue_code == "NO_WINDOWS" for i in result.issues))

    def test_environment_filtering(self):
        c = self.builder.make_component("svc-prod", env=EnvironmentType.PRODUCTION)
        manifest = self.builder.make_manifest([c])
        w = _make_window("WIN-DEV", "Dev Only", envs=["dev", "test"])
        engine = SchedulingEngine(manifest, [w])
        result = engine.schedule()
        self.assertEqual(result.total_unscheduled, 1)
        self.assertTrue(
            any("environment" in i.message.lower() for i in result.issues if i.component == "svc-prod")
        )

    def test_capacity_limit(self):
        components = [
            self.builder.make_component(f"svc-{i}", env=EnvironmentType.STAGING)
            for i in range(5)
        ]
        manifest = self.builder.make_manifest(components)
        w = _make_window("WIN-1", "Small Window", capacity=2, envs=["staging"])
        engine = SchedulingEngine(manifest, [w])
        result = engine.schedule()
        self.assertEqual(result.total_scheduled, 2)
        self.assertEqual(result.total_unscheduled, 3)

    def test_approval_required(self):
        c = self.builder.make_component("svc-a", env=EnvironmentType.PRODUCTION, has_approval=False)
        manifest = self.builder.make_manifest([c])
        w = _make_window("WIN-1", "Prod Window", envs=["production"], roles=["release-manager@corp.com"])
        policy = ReleasePolicy(
            policy_version="1.0",
            env_rules={
                "production": EnvironmentPolicy(require_approval=True),
            },
        )
        engine = SchedulingEngine(manifest, [w], policy=policy)
        result = engine.schedule()
        self.assertEqual(result.total_unscheduled, 1)
        self.assertTrue(
            any("approval" in i.message.lower() for i in result.issues)
        )

    def test_approval_satisfied(self):
        c = self.builder.make_component(
            "svc-a",
            env=EnvironmentType.PRODUCTION,
            has_approval=True,
            approver="release-manager@corp.com",
        )
        manifest = self.builder.make_manifest([c])
        w = _make_window("WIN-1", "Prod Window", envs=["production"], roles=["release-manager@corp.com"])
        policy = ReleasePolicy(
            policy_version="1.0",
            env_rules={
                "production": EnvironmentPolicy(require_approval=True),
            },
        )
        engine = SchedulingEngine(manifest, [w], policy=policy)
        result = engine.schedule()
        self.assertEqual(result.total_scheduled, 1)

    def test_dependency_ordering(self):
        a = self.builder.make_component("svc-a", env=EnvironmentType.STAGING)
        b = self.builder.make_component(
            "svc-b",
            env=EnvironmentType.STAGING,
            deps=[Dependency(name="svc-a", min_version="1.0.0", required=True)],
        )
        manifest = self.builder.make_manifest([b, a])
        w1 = _make_window("WIN-EARLY", "Early", start="2026-06-01T00:00:00Z", end="2026-06-01T12:00:00Z", envs=["staging"])
        w2 = _make_window("WIN-LATE", "Late", start="2026-06-02T00:00:00Z", end="2026-06-02T12:00:00Z", envs=["staging"])
        engine = SchedulingEngine(manifest, [w1, w2])
        result = engine.schedule()
        self.assertEqual(result.total_scheduled, 2)
        a_entry = next(e for e in result.entries if e.component_name == "svc-a")
        b_entry = next(e for e in result.entries if e.component_name == "svc-b")
        self.assertEqual(a_entry.window_id, "WIN-EARLY")
        self.assertEqual(b_entry.window_id, "WIN-EARLY")

    def test_freeze_period_blocks(self):
        c = self.builder.make_component("svc-a", env=EnvironmentType.STAGING)
        manifest = self.builder.make_manifest([c])
        fp = FreezePeriod(
            name="Freeze",
            start="2026-06-15T00:00:00Z",
            end="2026-06-16T00:00:00Z",
        )
        w = _make_window(
            "WIN-1",
            "Window",
            start="2026-06-15T09:00:00Z",
            end="2026-06-15T17:00:00Z",
            envs=["staging"],
            freezes=[fp],
        )
        engine = SchedulingEngine(manifest, [w])
        result = engine.schedule()
        self.assertEqual(result.total_unscheduled, 1)
        self.assertTrue(
            any("freeze" in i.message.lower() for i in result.issues if i.component == "svc-a")
        )

    def test_locked_window_blocks(self):
        c = self.builder.make_component("svc-a", env=EnvironmentType.STAGING)
        manifest = self.builder.make_manifest([c])
        w = _make_window("WIN-1", "Locked", envs=["staging"], locked=True)
        engine = SchedulingEngine(manifest, [w])
        result = engine.schedule()
        self.assertEqual(result.total_unscheduled, 1)
        self.assertTrue(
            any("locked" in i.message.lower() for i in result.issues if i.component == "svc-a")
        )

    def test_lock_unlock_operations(self):
        manifest = self.builder.make_manifest([])
        w = _make_window("WIN-1", "Window")
        engine = SchedulingEngine(manifest, [w])

        self.assertTrue(engine.lock_window("WIN-1", "admin@corp.com"))
        self.assertTrue(w.locked)
        self.assertEqual(w.locked_by, "admin@corp.com")
        self.assertIsNotNone(w.locked_at)

        self.assertFalse(engine.lock_window("WIN-1", "admin@corp.com"))

        self.assertTrue(engine.unlock_window("WIN-1", "admin@corp.com"))
        self.assertFalse(w.locked)
        self.assertIsNone(w.locked_by)

        self.assertFalse(engine.unlock_window("WIN-1", "admin@corp.com"))

        self.assertFalse(engine.lock_window("NOEXIST", "admin@corp.com"))

    def test_duplicate_window_ids(self):
        w1 = _make_window("WIN-1", "A")
        w2 = _make_window("WIN-1", "B")
        manifest = self.builder.make_manifest([])
        engine = SchedulingEngine(manifest, [w1, w2])
        result = engine.schedule()
        self.assertTrue(any(i.issue_code == "DUPLICATE_WINDOW_ID" for i in result.issues))

    def test_invalid_window_time(self):
        w = _make_window("WIN-1", "Bad", start="2026-06-15T17:00:00Z", end="2026-06-15T09:00:00Z")
        manifest = self.builder.make_manifest([])
        engine = SchedulingEngine(manifest, [w])
        result = engine.schedule()
        self.assertTrue(any(i.issue_code == "INVALID_WINDOW_TIME" for i in result.issues))

    def test_wave_ordering(self):
        a = self.builder.make_component("infra-a", env=EnvironmentType.STAGING)
        b = self.builder.make_component(
            "app-b",
            env=EnvironmentType.STAGING,
            deps=[Dependency(name="infra-a", min_version="1.0.0", required=True)],
        )
        manifest = self.builder.make_manifest([b, a])
        waves = [
            Wave(wave_id="W1", name="Infra", order=1),
            Wave(wave_id="W2", name="App", order=2),
        ]
        w = _make_window("WIN-1", "Window", envs=["staging"])
        engine = SchedulingEngine(manifest, [w], waves=waves)
        result = engine.schedule()
        self.assertEqual(result.total_scheduled, 2)
        a_entry = next(e for e in result.entries if e.component_name == "infra-a")
        b_entry = next(e for e in result.entries if e.component_name == "app-b")
        self.assertEqual(a_entry.wave_id, "W1")
        self.assertEqual(b_entry.wave_id, "W2")

    def test_exit_codes(self):
        manifest = self.builder.make_manifest([])
        engine = SchedulingEngine(manifest, [])
        engine.schedule()
        self.assertEqual(engine.determine_exit_code(), EXIT_CONFIG_ERROR.code)

        c = self.builder.make_component("svc-a", env=EnvironmentType.STAGING)
        manifest = self.builder.make_manifest([c])
        w = _make_window("WIN-1", "Window", envs=["production"])
        engine = SchedulingEngine(manifest, [w])
        engine.schedule()
        self.assertEqual(engine.determine_exit_code(), EXIT_SCHEDULE_ERROR.code)

        c = self.builder.make_component("svc-a", env=EnvironmentType.PRODUCTION, has_approval=False)
        manifest = self.builder.make_manifest([c])
        w = _make_window("WIN-1", "Window", envs=["production"], roles=["release-manager@corp.com"])
        policy = ReleasePolicy(
            policy_version="1.0",
            env_rules={
                "production": EnvironmentPolicy(require_approval=True),
            },
        )
        engine = SchedulingEngine(manifest, [w], policy=policy)
        engine.schedule()
        self.assertEqual(engine.determine_exit_code(), EXIT_APPROVAL_MISSING.code)

    def test_schedule_result_round_trip(self):
        c = self.builder.make_component("svc-a", env=EnvironmentType.STAGING)
        manifest = self.builder.make_manifest([c])
        w = _make_window("WIN-1", "Window", envs=["staging"])
        engine = SchedulingEngine(manifest, [w])
        result = engine.schedule()
        d = result.to_dict()
        self.assertEqual(d["total_scheduled"], 1)
        self.assertEqual(d["summary"]["windows"], 1)

        r2 = ScheduleResult.from_dict(d)
        self.assertEqual(r2.total_scheduled, 1)
        self.assertEqual(r2.windows[0].window_id, "WIN-1")


class TestLoggerIntegration(unittest.TestCase):
    def setUp(self):
        self.builder = _TestManifestBuilder()

    def tearDown(self):
        import glob
        for f in glob.glob(os.path.join(tempfile.gettempdir(), "test_*_*.pkg")):
            try:
                os.unlink(f)
            except OSError:
                pass

    def test_log_messages_recorded(self):
        from release_orchestrator.utils.logger import get_logger
        logger = get_logger(reset=True)
        c = self.builder.make_component("svc-log", env=EnvironmentType.STAGING)
        manifest = self.builder.make_manifest([c])
        w = _make_window("WIN-1", "Window", envs=["production"])
        engine = SchedulingEngine(manifest, [w])
        engine.schedule()
        entries = logger.get_entries()
        self.assertTrue(len(entries) > 0)
        self.assertTrue(any("SCHEDULE_FAILED" in e.get("message", "") for e in entries))
        log_text = logger.get_text()
        self.assertIn("svc-log", log_text)


if __name__ == "__main__":
    unittest.main()
