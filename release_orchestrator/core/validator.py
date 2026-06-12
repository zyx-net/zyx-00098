"""Validation engine for release components.

Performs all pre-release checks including:
- Circular dependency detection
- Version downgrade detection
- Checksum verification (against declared checksum)
- Approval record verification (production env)
- Dependency constraint validation
"""
from __future__ import annotations

from collections import defaultdict, deque
from typing import Any, Dict, List, Optional, Set, Tuple

from .models import (
    ApprovalStatus,
    Component,
    EnvironmentType,
    ReleaseManifest,
    Severity,
    ValidationIssue,
    ValidationResult,
    compare_versions,
    compute_checksum,
    now_iso,
)
from .policy import EnvironmentPolicy, ReleasePolicy, default_policy
from ..utils.logger import get_logger
from ..utils.exit_codes import (
    EXIT_APPROVAL_MISSING,
    EXIT_CHECKSUM_MISMATCH,
    EXIT_CIRCULAR_DEPENDENCY,
    EXIT_VERSION_DOWNGRADE,
)

LOG = get_logger()
MODULE = "validator"


class ValidationEngine:
    """Executes all validation rules against a release manifest."""

    def __init__(self, manifest: ReleaseManifest, policy: Optional[ReleasePolicy] = None):
        self.manifest = manifest
        self.policy: ReleasePolicy = policy if policy is not None else default_policy()
        self.issues: List[ValidationIssue] = []
        self._components_by_name: Dict[str, Component] = {
            c.name: c for c in manifest.components
        }
        env_name = (
            manifest.target_environment.value
            if isinstance(manifest.target_environment, EnvironmentType)
            else str(manifest.target_environment)
        )
        self.env_policy: EnvironmentPolicy = self.policy.get_env_policy(env_name)
        self.policy_warnings: List[str] = []
        self._check_policy_components()

    def add_issue(
        self,
        severity: Severity,
        code: str,
        message: str,
        component: Optional[str] = None,
        details: Optional[Dict[str, Any]] = None,
    ) -> None:
        issue = ValidationIssue(
            component=component,
            severity=severity,
            issue_code=code,
            message=message,
            details=details,
        )
        self.issues.append(issue)
        level = {
            Severity.ERROR: "error",
            Severity.WARNING: "warning",
            Severity.INFO: "info",
        }[severity]
        getattr(LOG, level)(MODULE, f"[{code}] {message}", component=component or "-")

    def validate(self, verify_checksums: bool = True) -> ValidationResult:
        """Run the full validation suite."""
        LOG.info(MODULE, "Starting validation", release_id=self.manifest.release_id)
        self._validate_basic_structure()
        self._detect_circular_dependencies()
        self._check_version_downgrades()
        self._validate_dependency_constraints()
        if verify_checksums:
            self._verify_checksums()
        self._verify_approvals()

        result = ValidationResult(
            timestamp=now_iso(),
            issues=self.issues,
            passed=not any(i.severity == Severity.ERROR for i in self.issues),
        )
        summary = result.to_dict()["summary"]
        LOG.info(
            MODULE,
            "Validation complete",
            passed=result.passed,
            total=summary["total"],
            errors=summary["errors"],
            warnings=summary["warnings"],
        )
        return result

    def determine_exit_code(self) -> int:
        """Map validation errors to specific exit codes (priority order)."""
        codes = {i.issue_code for i in self.issues if i.severity == Severity.ERROR}
        if "CIRCULAR_DEPENDENCY" in codes:
            return EXIT_CIRCULAR_DEPENDENCY.code
        if "VERSION_DOWNGRADE" in codes:
            return EXIT_VERSION_DOWNGRADE.code
        if "CHECKSUM_MISMATCH" in codes:
            return EXIT_CHECKSUM_MISMATCH.code
        if "APPROVAL_MISSING" in codes:
            return EXIT_APPROVAL_MISSING.code
        if "POLICY_CONFLICT" in codes:
            return EXIT_CONFIG_ERROR.code
        if codes:
            return 12
        return 0

    def _check_policy_components(self) -> None:
        """Validate that policy-referenced components exist in the manifest."""
        manifest_names = set(self._components_by_name.keys())
        for comp_name in self.env_policy.skip_checksum_components:
            if comp_name not in manifest_names:
                msg = (
                    f"Policy skip_checksum_components references unknown component "
                    f"'{comp_name}' for target environment"
                )
                self.policy_warnings.append(msg)
                self.add_issue(
                    Severity.WARNING,
                    "POLICY_UNKNOWN_COMPONENT",
                    msg,
                    component=comp_name,
                )

    # --- individual check methods ---

    def _validate_basic_structure(self) -> None:
        if not self.manifest.release_id:
            self.add_issue(Severity.WARNING, "NO_RELEASE_ID", "Manifest has no release_id")
        if not self.manifest.components:
            self.add_issue(Severity.WARNING, "NO_COMPONENTS", "Manifest contains no components")
        seen: Set[Tuple[str, str]] = set()
        for c in self.manifest.components:
            key = (c.name, c.version)
            if key in seen:
                self.add_issue(
                    Severity.ERROR,
                    "DUPLICATE_COMPONENT",
                    f"Duplicate component: {c.name} v{c.version}",
                    component=c.name,
                )
            seen.add(key)

    def _detect_circular_dependencies(self) -> None:
        graph: Dict[str, List[str]] = defaultdict(list)
        for c in self.manifest.components:
            for dep in c.dependencies:
                if dep.name in self._components_by_name and dep.required:
                    graph[c.name].append(dep.name)

        WHITE, GRAY, BLACK = 0, 1, 2
        color = {name: WHITE for name in self._components_by_name}
        cycles: List[List[str]] = []

        def dfs(node: str, path: List[str]) -> None:
            color[node] = GRAY
            path.append(node)
            for nxt in graph.get(node, []):
                if color.get(nxt) == GRAY:
                    idx = path.index(nxt)
                    cycle = path[idx:] + [nxt]
                    cycles.append(cycle)
                elif color.get(nxt) == WHITE:
                    dfs(nxt, path)
            path.pop()
            color[node] = BLACK

        for name in list(self._components_by_name.keys()):
            if color[name] == WHITE:
                dfs(name, [])

        for cycle in cycles:
            self.add_issue(
                Severity.ERROR,
                "CIRCULAR_DEPENDENCY",
                f"Circular dependency detected: {' -> '.join(cycle)}",
                component=cycle[0] if cycle else None,
                details={"cycle": cycle},
            )

    def _check_version_downgrades(self) -> None:
        allow_downgrade = self.env_policy.allow_version_downgrade
        for c in self.manifest.components:
            if not c.deployed_version:
                self.add_issue(
                    Severity.INFO,
                    "NO_DEPLOYED_VERSION",
                    f"No deployed version recorded for {c.name}",
                    component=c.name,
                )
                continue
            cmp = compare_versions(c.version, c.deployed_version)
            if cmp < 0:
                if allow_downgrade:
                    self.add_issue(
                        Severity.INFO,
                        "VERSION_DOWNGRADE_ALLOWED",
                        f"Version downgrade for {c.name}: {c.deployed_version} -> {c.version} (allowed by policy)",
                        component=c.name,
                        details={"from": c.deployed_version, "to": c.version, "policy": "allow_version_downgrade"},
                    )
                else:
                    self.add_issue(
                        Severity.ERROR,
                        "VERSION_DOWNGRADE",
                        f"Version downgrade for {c.name}: {c.deployed_version} -> {c.version}",
                        component=c.name,
                        details={"from": c.deployed_version, "to": c.version},
                    )
            elif cmp == 0:
                self.add_issue(
                    Severity.WARNING,
                    "VERSION_SAME",
                    f"Version unchanged for {c.name}: {c.version}",
                    component=c.name,
                )

    def _validate_dependency_constraints(self) -> None:
        for c in self.manifest.components:
            for dep in c.dependencies:
                dep_comp = self._components_by_name.get(dep.name)
                if not dep_comp:
                    if dep.required:
                        self.add_issue(
                            Severity.ERROR,
                            "DEPENDENCY_MISSING",
                            f"Component {c.name} requires missing dependency {dep.name}",
                            component=c.name,
                            details={"dependency": dep.name},
                        )
                    else:
                        self.add_issue(
                            Severity.WARNING,
                            "OPTIONAL_DEP_MISSING",
                            f"Optional dependency {dep.name} for {c.name} not in manifest",
                            component=c.name,
                        )
                    continue
                if dep.min_version:
                    if compare_versions(dep_comp.version, dep.min_version) < 0:
                        self.add_issue(
                            Severity.ERROR,
                            "DEP_VERSION_TOO_LOW",
                            f"{c.name} requires {dep.name} >= {dep.min_version}, got {dep_comp.version}",
                            component=c.name,
                            details={"dep": dep.name, "need": dep.min_version, "got": dep_comp.version},
                        )
                if dep.max_version:
                    if compare_versions(dep_comp.version, dep.max_version) > 0:
                        self.add_issue(
                            Severity.WARNING,
                            "DEP_VERSION_ABOVE_MAX",
                            f"{c.name} prefers {dep.name} <= {dep.max_version}, got {dep_comp.version}",
                            component=c.name,
                        )

    def _verify_checksums(self) -> None:
        import os
        skip_components = set(self.env_policy.skip_checksum_components)
        for c in self.manifest.components:
            if c.name in skip_components:
                self.add_issue(
                    Severity.INFO,
                    "CHECKSUM_SKIPPED_BY_POLICY",
                    f"Checksum verification skipped for {c.name} by policy",
                    component=c.name,
                    details={"policy": "skip_checksum_components"},
                )
                continue
            art = c.artifact
            if not art.path or not art.checksum:
                self.add_issue(
                    Severity.WARNING,
                    "CHECKSUM_DECLARATION_INCOMPLETE",
                    f"Component {c.name} has incomplete artifact/path declaration",
                    component=c.name,
                )
                continue
            if not os.path.exists(art.path):
                self.add_issue(
                    Severity.WARNING,
                    "ARTIFACT_FILE_MISSING",
                    f"Artifact file not found on disk for {c.name} (offline mode, skipping content verification): {art.path}",
                    component=c.name,
                    details={"path": art.path},
                )
                continue
            try:
                with open(art.path, "rb") as f:
                    actual = compute_checksum(f.read(), art.checksum_algorithm or "sha256")
                if actual.lower() != art.checksum.lower():
                    self.add_issue(
                        Severity.ERROR,
                        "CHECKSUM_MISMATCH",
                        f"Checksum mismatch for {c.name}: expected {art.checksum}, got {actual}",
                        component=c.name,
                        details={
                            "expected": art.checksum,
                            "actual": actual,
                            "algorithm": art.checksum_algorithm,
                        },
                    )
                else:
                    self.add_issue(
                        Severity.INFO,
                        "CHECKSUM_OK",
                        f"Checksum verified OK for {c.name}",
                        component=c.name,
                    )
            except Exception as exc:
                self.add_issue(
                    Severity.ERROR,
                    "CHECKSUM_READ_ERROR",
                    f"Failed to read artifact for {c.name}: {exc}",
                    component=c.name,
                )

    def _verify_approvals(self) -> None:
        require_approval = self.env_policy.require_approval
        if not require_approval:
            for c in self.manifest.components:
                approved = [a for a in c.approvals if a.status == ApprovalStatus.APPROVED]
                if approved:
                    approvers = [a.approver for a in approved]
                    self.add_issue(
                        Severity.INFO,
                        "APPROVAL_OPTIONAL",
                        f"Component {c.name} has {len(approved)} approval(s) (approval not required by policy)",
                        component=c.name,
                        details={"approvers": approvers},
                    )
            return

        env = self.manifest.target_environment
        for c in self.manifest.components:
            approved = [a for a in c.approvals if a.status == ApprovalStatus.APPROVED]
            if not approved:
                self.add_issue(
                    Severity.ERROR,
                    "APPROVAL_MISSING",
                    f"Component {c.name} ({c.environment.value}) has no APPROVED approval record (required by policy)",
                    component=c.name,
                    details={"approvals_count": len(c.approvals), "policy": "require_approval"},
                )
            else:
                approvers = [a.approver for a in approved]
                self.add_issue(
                    Severity.INFO,
                    "APPROVAL_OK",
                    f"Component {c.name} approved by: {', '.join(approvers)}",
                    component=c.name,
                    details={"approvers": approvers},
                )
