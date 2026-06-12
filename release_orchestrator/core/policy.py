"""Release policy configuration model.

Defines environment-level preflight policy rules that govern how
validation, planning, dry-run, and export commands behave.  Policies
are loaded from JSON files and can be overridden via CLI arguments.
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any, Dict, List, Optional

from .models import EnvironmentType


POLICY_VERSION = "1.0"
DEFAULT_POLICY_FILE = "release_policy.json"


@dataclass
class EnvironmentPolicy:
    """Policy rules for a specific target environment."""

    require_approval: bool = False
    allow_version_downgrade: bool = False
    skip_checksum_components: List[str] = field(default_factory=list)
    dry_run_failure_blocks_export: bool = True

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "EnvironmentPolicy":
        return cls(
            require_approval=bool(data.get("require_approval", False)),
            allow_version_downgrade=bool(data.get("allow_version_downgrade", False)),
            skip_checksum_components=list(data.get("skip_checksum_components", [])),
            dry_run_failure_blocks_export=bool(data.get("dry_run_failure_blocks_export", True)),
        )


@dataclass
class ReleasePolicy:
    """Full release policy configuration.

    Contains environment-specific rules plus global policy metadata.
    """

    policy_version: str = POLICY_VERSION
    default_environment: str = "production"
    env_rules: Dict[str, EnvironmentPolicy] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "policy_version": self.policy_version,
            "default_environment": self.default_environment,
            "env_rules": {k: v.to_dict() for k, v in self.env_rules.items()},
        }

    def to_json(self, indent: int = 2) -> str:
        import json
        return json.dumps(self.to_dict(), indent=indent, ensure_ascii=False)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ReleasePolicy":
        env_rules_data = data.get("env_rules", {})
        env_rules: Dict[str, EnvironmentPolicy] = {}
        for env_name, env_data in env_rules_data.items():
            env_rules[env_name] = EnvironmentPolicy.from_dict(env_data)
        return cls(
            policy_version=data.get("policy_version", POLICY_VERSION),
            default_environment=data.get("default_environment", "production"),
            env_rules=env_rules,
        )

    @classmethod
    def from_json(cls, content: str) -> "ReleasePolicy":
        import json
        return cls.from_dict(json.loads(content))

    def get_env_policy(self, environment: str) -> EnvironmentPolicy:
        """Get the policy rules for a given environment name.

        Falls back to the default environment if no specific rules exist.
        """
        if environment in self.env_rules:
            return self.env_rules[environment]
        default_env = self.default_environment
        if default_env in self.env_rules:
            return self.env_rules[default_env]
        return EnvironmentPolicy()

    def list_known_environments(self) -> List[str]:
        """Return all environment names defined in the policy."""
        return list(self.env_rules.keys())


def default_policy() -> ReleasePolicy:
    """Return a sensible default policy.

    - dev:      no approval required, downgrades allowed, no checksum skip,
                dry-run failure does NOT block export
    - test:     no approval required, downgrades not allowed,
                dry-run failure blocks export
    - staging:  approval required, downgrades not allowed,
                dry-run failure blocks export
    - production: approval required, downgrades not allowed,
                  no components skip checksum, dry-run blocks export
    """
    return ReleasePolicy(
        policy_version=POLICY_VERSION,
        default_environment="production",
        env_rules={
            "dev": EnvironmentPolicy(
                require_approval=False,
                allow_version_downgrade=True,
                skip_checksum_components=[],
                dry_run_failure_blocks_export=False,
            ),
            "test": EnvironmentPolicy(
                require_approval=False,
                allow_version_downgrade=False,
                skip_checksum_components=[],
                dry_run_failure_blocks_export=True,
            ),
            "staging": EnvironmentPolicy(
                require_approval=True,
                allow_version_downgrade=False,
                skip_checksum_components=[],
                dry_run_failure_blocks_export=True,
            ),
            "production": EnvironmentPolicy(
                require_approval=True,
                allow_version_downgrade=False,
                skip_checksum_components=[],
                dry_run_failure_blocks_export=True,
            ),
        },
    )
