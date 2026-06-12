"""Core data models for the release orchestrator.

Defines the schema for components, dependencies, approval records,
deployed states, manifests, validation results, release plans, and
execution history.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field, asdict
from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple


class EnvironmentType(str, Enum):
    DEV = "dev"
    TEST = "test"
    STAGING = "staging"
    PRODUCTION = "production"


class ComponentStatus(str, Enum):
    PENDING = "pending"
    SCHEDULED = "scheduled"
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"
    SKIPPED = "skipped"
    BLOCKED = "blocked"


class ApprovalStatus(str, Enum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"


class Severity(str, Enum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


@dataclass
class Dependency:
    name: str
    min_version: Optional[str] = None
    max_version: Optional[str] = None
    required: bool = True

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Dependency":
        return cls(**data)


@dataclass
class ApprovalRecord:
    approver: str
    status: ApprovalStatus
    timestamp: str
    comment: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["status"] = self.status.value if isinstance(self.status, ApprovalStatus) else self.status
        return d

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ApprovalRecord":
        status = data.get("status")
        if isinstance(status, str):
            status = ApprovalStatus(status)
        return cls(
            approver=data["approver"],
            status=status,
            timestamp=data["timestamp"],
            comment=data.get("comment"),
        )


@dataclass
class PackageArtifact:
    path: str
    checksum: str
    checksum_algorithm: str = "sha256"
    size_bytes: Optional[int] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "PackageArtifact":
        return cls(**data)


@dataclass
class Component:
    name: str
    version: str
    environment: EnvironmentType
    artifact: PackageArtifact
    dependencies: List[Dependency] = field(default_factory=list)
    approvals: List[ApprovalRecord] = field(default_factory=list)
    deployed_version: Optional[str] = None
    description: Optional[str] = None
    rollback_target_version: Optional[str] = None
    tags: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["environment"] = self.environment.value if isinstance(self.environment, EnvironmentType) else self.environment
        d["artifact"] = self.artifact.to_dict() if isinstance(self.artifact, PackageArtifact) else self.artifact
        d["dependencies"] = [dep.to_dict() if isinstance(dep, Dependency) else dep for dep in self.dependencies]
        d["approvals"] = [a.to_dict() if isinstance(a, ApprovalRecord) else a for a in self.approvals]
        return d

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Component":
        env = data.get("environment")
        if isinstance(env, str):
            env = EnvironmentType(env)
        artifact = data.get("artifact", {})
        if isinstance(artifact, dict):
            artifact = PackageArtifact.from_dict(artifact)
        deps = [
            Dependency.from_dict(d) if isinstance(d, dict) else d
            for d in data.get("dependencies", [])
        ]
        approvals = [
            ApprovalRecord.from_dict(a) if isinstance(a, dict) else a
            for a in data.get("approvals", [])
        ]
        return cls(
            name=data["name"],
            version=data["version"],
            environment=env,
            artifact=artifact,
            dependencies=deps,
            approvals=approvals,
            deployed_version=data.get("deployed_version"),
            description=data.get("description"),
            rollback_target_version=data.get("rollback_target_version"),
            tags=data.get("tags", []),
        )


@dataclass
class ReleaseManifest:
    manifest_version: str
    release_id: str
    title: str
    target_environment: EnvironmentType
    scheduled_date: Optional[str] = None
    components: List[Component] = field(default_factory=list)
    description: Optional[str] = None
    created_by: Optional[str] = None
    created_at: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["target_environment"] = (
            self.target_environment.value
            if isinstance(self.target_environment, EnvironmentType)
            else self.target_environment
        )
        d["components"] = [c.to_dict() if isinstance(c, Component) else c for c in self.components]
        return d

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent, ensure_ascii=False)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ReleaseManifest":
        env = data.get("target_environment")
        if isinstance(env, str):
            env = EnvironmentType(env)
        components = [
            Component.from_dict(c) if isinstance(c, dict) else c
            for c in data.get("components", [])
        ]
        return cls(
            manifest_version=data.get("manifest_version", "1.0"),
            release_id=data.get("release_id", ""),
            title=data.get("title", ""),
            target_environment=env,
            scheduled_date=data.get("scheduled_date"),
            components=components,
            description=data.get("description"),
            created_by=data.get("created_by"),
            created_at=data.get("created_at"),
        )

    @classmethod
    def from_json(cls, content: str) -> "ReleaseManifest":
        return cls.from_dict(json.loads(content))


@dataclass
class ValidationIssue:
    component: Optional[str]
    severity: Severity
    issue_code: str
    message: str
    details: Optional[Dict[str, Any]] = None

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["severity"] = self.severity.value if isinstance(self.severity, Severity) else self.severity
        return d

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ValidationIssue":
        sev = data.get("severity")
        if isinstance(sev, str):
            sev = Severity(sev)
        return cls(
            component=data.get("component"),
            severity=sev,
            issue_code=data["issue_code"],
            message=data["message"],
            details=data.get("details"),
        )


@dataclass
class ValidationResult:
    timestamp: str
    issues: List[ValidationIssue] = field(default_factory=list)
    passed: bool = True

    def has_errors(self) -> bool:
        return any(issue.severity == Severity.ERROR for issue in self.issues)

    def errors(self) -> List[ValidationIssue]:
        return [i for i in self.issues if i.severity == Severity.ERROR]

    def warnings(self) -> List[ValidationIssue]:
        return [i for i in self.issues if i.severity == Severity.WARNING]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "timestamp": self.timestamp,
            "passed": self.passed and not self.has_errors(),
            "issues": [i.to_dict() for i in self.issues],
            "summary": {
                "total": len(self.issues),
                "errors": len(self.errors()),
                "warnings": len(self.warnings()),
                "infos": len([i for i in self.issues if i.severity == Severity.INFO]),
            },
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ValidationResult":
        issues = [
            ValidationIssue.from_dict(i) if isinstance(i, dict) else i
            for i in data.get("issues", [])
        ]
        return cls(
            timestamp=data["timestamp"],
            issues=issues,
            passed=data.get("passed", True),
        )


@dataclass
class PlanStep:
    step_index: int
    component_name: str
    component_version: str
    action: str
    prerequisites: List[str] = field(default_factory=list)
    blockers: List[str] = field(default_factory=list)
    approver: Optional[str] = None
    rollback_target: Optional[str] = None
    status: ComponentStatus = ComponentStatus.PENDING
    estimated_duration_minutes: int = 5
    notes: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["status"] = self.status.value if isinstance(self.status, ComponentStatus) else self.status
        return d

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "PlanStep":
        status = data.get("status")
        if isinstance(status, str):
            status = ComponentStatus(status)
        return cls(
            step_index=data["step_index"],
            component_name=data["component_name"],
            component_version=data["component_version"],
            action=data["action"],
            prerequisites=data.get("prerequisites", []),
            blockers=data.get("blockers", []),
            approver=data.get("approver"),
            rollback_target=data.get("rollback_target"),
            status=status,
            estimated_duration_minutes=data.get("estimated_duration_minutes", 5),
            notes=data.get("notes"),
        )


@dataclass
class ReleasePlan:
    plan_id: str
    release_id: str
    generated_at: str
    target_environment: EnvironmentType
    execution_order: List[str] = field(default_factory=list)
    steps: List[PlanStep] = field(default_factory=list)
    blocked_components: List[Dict[str, Any]] = field(default_factory=list)
    total_estimated_minutes: int = 0

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["target_environment"] = (
            self.target_environment.value
            if isinstance(self.target_environment, EnvironmentType)
            else self.target_environment
        )
        d["steps"] = [s.to_dict() for s in self.steps]
        return d

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ReleasePlan":
        env = data.get("target_environment")
        if isinstance(env, str):
            env = EnvironmentType(env)
        steps = [
            PlanStep.from_dict(s) if isinstance(s, dict) else s
            for s in data.get("steps", [])
        ]
        return cls(
            plan_id=data["plan_id"],
            release_id=data["release_id"],
            generated_at=data["generated_at"],
            target_environment=env,
            execution_order=data.get("execution_order", []),
            steps=steps,
            blocked_components=data.get("blocked_components", []),
            total_estimated_minutes=data.get("total_estimated_minutes", 0),
        )


@dataclass
class LogEntry:
    timestamp: str
    level: str
    module: str
    message: str
    extra: Optional[Dict[str, Any]] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "LogEntry":
        return cls(
            timestamp=data["timestamp"],
            level=data["level"],
            module=data["module"],
            message=data["message"],
            extra=data.get("extra"),
        )


@dataclass
class ExecutionSnapshot:
    run_id: str
    command: str
    started_at: str
    finished_at: Optional[str] = None
    exit_code: int = 0
    config_snapshot: Optional[Dict[str, Any]] = None
    manifest_snapshot: Optional[Dict[str, Any]] = None
    validation_result: Optional[Dict[str, Any]] = None
    release_plan: Optional[Dict[str, Any]] = None
    rollback_plan: Optional[Dict[str, Any]] = None
    dry_run_result: Optional[Dict[str, Any]] = None
    logs: List[Dict[str, Any]] = field(default_factory=list)
    archive_path: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ExecutionSnapshot":
        return cls(
            run_id=data["run_id"],
            command=data["command"],
            started_at=data["started_at"],
            finished_at=data.get("finished_at"),
            exit_code=data.get("exit_code", 0),
            config_snapshot=data.get("config_snapshot"),
            manifest_snapshot=data.get("manifest_snapshot"),
            validation_result=data.get("validation_result"),
            release_plan=data.get("release_plan"),
            rollback_plan=data.get("rollback_plan"),
            dry_run_result=data.get("dry_run_result"),
            logs=data.get("logs", []),
            archive_path=data.get("archive_path"),
        )


def compare_versions(v1: str, v2: str) -> int:
    """Compare two semantic version strings.

    Returns:
        -1 if v1 < v2, 0 if v1 == v2, 1 if v1 > v2.
    """
    def _parse(v: str) -> List[int]:
        parts = []
        for p in v.replace("-", ".").replace("_", ".").split("."):
            try:
                parts.append(int(p))
            except ValueError:
                parts.append(0)
        while len(parts) < 3:
            parts.append(0)
        return parts

    p1, p2 = _parse(v1), _parse(v2)
    length = max(len(p1), len(p2))
    for i in range(length):
        a = p1[i] if i < len(p1) else 0
        b = p2[i] if i < len(p2) else 0
        if a < b:
            return -1
        if a > b:
            return 1
    return 0


def compute_checksum(content: bytes, algorithm: str = "sha256") -> str:
    """Compute checksum of byte content using given algorithm.

    Args:
        content: The byte content to hash.
        algorithm: The hash algorithm (md5, sha1, sha256, sha512).

    Returns:
        Hex digest string.
    """
    h = hashlib.new(algorithm)
    h.update(content)
    return h.hexdigest()


def now_iso() -> str:
    """Return current UTC timestamp as ISO-8601 string."""
    return datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")


def generate_id(prefix: str) -> str:
    """Generate a short unique identifier with a prefix."""
    ts = datetime.utcnow().strftime("%Y%m%d%H%M%S")
    rand = hashlib.md5(ts.encode()).hexdigest()[:8]
    return f"{prefix}-{ts}-{rand}"
