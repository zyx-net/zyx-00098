"""Policy loading, validation, and evaluation utilities.

Handles loading release policies from JSON files, applying defaults,
validating policy structure, and computing policy-hit summaries.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from ..core.models import ReleaseManifest, Component
from ..core.policy import (
    DEFAULT_POLICY_FILE,
    EnvironmentPolicy,
    ReleasePolicy,
    default_policy,
)
from ..utils.exit_codes import EXIT_CONFIG_ERROR
from ..utils.logger import get_logger
from .storage import get_work_dir


LOG = get_logger()
MODULE = "policy_loader"


class PolicyValidationError(Exception):
    """Raised when a policy file has structural or value errors."""

    def __init__(self, message: str, errors: Optional[List[str]] = None):
        super().__init__(message)
        self.errors = errors or [message]


def load_policy(
    policy_path: Optional[str] = None,
    work_dir: Optional[str] = None,
) -> ReleasePolicy:
    """Load a release policy.

    Resolution order:
    1. Explicit ``policy_path`` argument if provided
    2. Work directory default: ``<work_dir>/release_policy.json``
    3. Current working directory default: ``./release_policy.json``
    4. Built-in default policy

    Args:
        policy_path: Explicit path to a policy JSON file.
        work_dir: Base work directory (used to find default policy).

    Returns:
        A validated ReleasePolicy object.

    Raises:
        PolicyValidationError: If the policy file exists but is invalid.
        FileNotFoundError: If an explicit policy_path is provided but not found.
    """
    if policy_path:
        p = Path(policy_path)
        if not p.exists():
            raise FileNotFoundError(f"Policy file not found: {policy_path}")
        LOG.info(MODULE, "Loading policy from explicit path", path=str(p))
        return _load_and_validate(p)

    candidates: List[Path] = []
    if work_dir:
        work_default = Path(work_dir) / DEFAULT_POLICY_FILE
        candidates.append(work_default)

    cwd_default = Path.cwd() / DEFAULT_POLICY_FILE
    if cwd_default not in candidates:
        candidates.append(cwd_default)

    for candidate in candidates:
        if candidate.exists():
            LOG.info(MODULE, "Loading default policy from workspace", path=str(candidate))
            return _load_and_validate(candidate)

    LOG.info(MODULE, "No policy file found - using built-in defaults")
    return default_policy()


def _load_and_validate(path: Path) -> ReleasePolicy:
    """Load a policy file and validate its structure."""
    try:
        with path.open("r", encoding="utf-8") as f:
            data = json.load(f)
    except json.JSONDecodeError as exc:
        raise PolicyValidationError(
            f"Invalid policy JSON in {path}: {exc}",
            errors=[f"JSON parse error: {exc}"],
        )

    errors = validate_policy_data(data)
    if errors:
        raise PolicyValidationError(
            f"Policy file {path} has {len(errors)} validation error(s)",
            errors=errors,
        )

    policy = ReleasePolicy.from_dict(data)
    LOG.info(
        MODULE,
        "Policy loaded successfully",
        policy_version=policy.policy_version,
        environments=len(policy.env_rules),
    )
    return policy


def validate_policy_data(data: Dict[str, Any]) -> List[str]:
    """Validate raw policy data structure.

    Checks for:
    - Missing required top-level fields
    - Wrong types for boolean fields
    - Non-list skip_checksum_components
    - Invalid environment names
    - Unknown fields (warnings, not errors)

    Returns:
        A list of error message strings. Empty means valid.
    """
    errors: List[str] = []

    if not isinstance(data, dict):
        return ["Policy root must be a JSON object"]

    if "policy_version" in data:
        if not isinstance(data["policy_version"], str):
            errors.append("policy_version must be a string")
    else:
        errors.append("Missing required field: policy_version")

    if "default_environment" in data:
        if not isinstance(data["default_environment"], str):
            errors.append("default_environment must be a string")
    else:
        errors.append("Missing required field: default_environment")

    env_rules = data.get("env_rules")
    if env_rules is None:
        errors.append("Missing required field: env_rules")
    elif not isinstance(env_rules, dict):
        errors.append("env_rules must be an object mapping env names to rules")
    else:
        for env_name, env_data in env_rules.items():
            if not isinstance(env_name, str):
                errors.append(f"env_rules key must be a string, got {type(env_name).__name__}")
                continue
            if not isinstance(env_data, dict):
                errors.append(f"env_rules['{env_name}'] must be an object")
                continue

            bool_fields = [
                "require_approval",
                "allow_version_downgrade",
                "dry_run_failure_blocks_export",
            ]
            for field in bool_fields:
                if field in env_data:
                    val = env_data[field]
                    if not isinstance(val, bool):
                        errors.append(
                            f"env_rules['{env_name}'].{field} must be a boolean, "
                            f"got {type(val).__name__}: {val!r}"
                        )

            if "skip_checksum_components" in env_data:
                skip_list = env_data["skip_checksum_components"]
                if not isinstance(skip_list, list):
                    errors.append(
                        f"env_rules['{env_name}'].skip_checksum_components "
                        f"must be a list, got {type(skip_list).__name__}"
                    )
                else:
                    for i, item in enumerate(skip_list):
                        if not isinstance(item, str):
                            errors.append(
                                f"env_rules['{env_name}'].skip_checksum_components[{i}] "
                                f"must be a string, got {type(item).__name__}"
                            )

    return errors


def evaluate_policy(
    policy: ReleasePolicy,
    manifest: ReleaseManifest,
) -> Dict[str, Any]:
    """Evaluate policy against a manifest and produce a hit summary.

    The summary records which policy rules apply to the current manifest
    and which components are affected by specific rules (e.g. checksum skip).

    Args:
        policy: The release policy to evaluate.
        manifest: The release manifest to evaluate against.

    Returns:
        A dict with policy evaluation results including:
        - target_environment: the environment used for policy lookup
        - rules_applied: list of rule names that are active for this env
        - component_impact: per-component rule hits
        - warnings: list of warning strings (e.g. unknown component names in skip list)
    """
    env_name = manifest.target_environment.value if hasattr(manifest.target_environment, "value") else str(manifest.target_environment)
    env_policy = policy.get_env_policy(env_name)

    warnings: List[str] = []
    component_impact: Dict[str, List[str]] = {}
    manifest_component_names = {c.name for c in manifest.components}

    for comp_name in env_policy.skip_checksum_components:
        if comp_name not in manifest_component_names:
            warnings.append(
                f"Policy skip_checksum_components references unknown component '{comp_name}' "
                f"in environment '{env_name}'"
            )

    rules_applied: List[str] = []
    if env_policy.require_approval:
        rules_applied.append("require_approval")
    if not env_policy.allow_version_downgrade:
        rules_applied.append("block_version_downgrade")
    if env_policy.skip_checksum_components:
        rules_applied.append("skip_checksum_for_components")
    if env_policy.dry_run_failure_blocks_export:
        rules_applied.append("dry_run_failure_blocks_export")

    for comp in manifest.components:
        impacts: List[str] = []
        if env_policy.require_approval:
            impacts.append("approval_required")
        if not env_policy.allow_version_downgrade:
            impacts.append("version_downgrade_blocked")
        if comp.name in env_policy.skip_checksum_components:
            impacts.append("checksum_skipped")
        if impacts:
            component_impact[comp.name] = impacts

    return {
        "target_environment": env_name,
        "policy_version": policy.policy_version,
        "default_environment": policy.default_environment,
        "rules_applied": rules_applied,
        "component_impact": component_impact,
        "warnings": warnings,
        "env_rules": env_policy.to_dict(),
    }


def save_default_policy(path: str) -> Path:
    """Write the built-in default policy to a JSON file.

    Args:
        path: Output file path.

    Returns:
        The Path of the written file.
    """
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    policy = default_policy()
    with p.open("w", encoding="utf-8") as f:
        f.write(policy.to_json())
    LOG.info(MODULE, "Wrote default policy file", path=str(p))
    return p
