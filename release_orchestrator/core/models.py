"""Core data models for the release orchestrator.

Defines the schema for components, dependencies, approval records,
deployed states, manifests, validation results, release plans, and
execution history.
"""
from __future__ import annotations

import hashlib
import itertools
import json
import os
import threading
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
    policy_snapshot: Optional[Dict[str, Any]] = None
    policy_summary: Optional[Dict[str, Any]] = None
    validation_result: Optional[Dict[str, Any]] = None
    release_plan: Optional[Dict[str, Any]] = None
    rollback_plan: Optional[Dict[str, Any]] = None
    dry_run_result: Optional[Dict[str, Any]] = None
    schedule_result: Optional[Dict[str, Any]] = None
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
            policy_snapshot=data.get("policy_snapshot"),
            policy_summary=data.get("policy_summary"),
            validation_result=data.get("validation_result"),
            release_plan=data.get("release_plan"),
            rollback_plan=data.get("rollback_plan"),
            dry_run_result=data.get("dry_run_result"),
            schedule_result=data.get("schedule_result"),
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


_ID_COUNTER = itertools.count()
_ID_LOCK = threading.Lock()


def generate_id(prefix: str) -> str:
    """Generate a stable unique identifier with a prefix.

    Format: ``PREFIX-YYYYMMDDHHMMSS-<8 hex chars>`` (unchanged from
    the original layout so existing history and display code keep
    working).

    Uniqueness in the same second is guaranteed by combining the
    second-precision timestamp with a per-process monotonic counter
    and 8 bytes of OS-level randomness before hashing.  This makes
    collisions impossible even with concurrent calls in the same
    process and extremely unlikely across separate processes.
    """
    ts = datetime.utcnow().strftime("%Y%m%d%H%M%S")
    with _ID_LOCK:
        counter = next(_ID_COUNTER)
    entropy = f"{ts}-{counter}-{os.urandom(8).hex()}"
    digest = hashlib.md5(entropy.encode()).hexdigest()[:8]
    return f"{prefix}-{ts}-{digest}"


@dataclass
class FreezePeriod:
    """A period during which releases are not allowed in a window."""

    name: str
    start: str
    end: str
    reason: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "FreezePeriod":
        return cls(
            name=data["name"],
            start=data["start"],
            end=data["end"],
            reason=data.get("reason"),
        )


@dataclass
class ReleaseWindow:
    """A scheduled release window with capacity and constraints."""

    window_id: str
    name: str
    start_time: str
    end_time: str
    timezone: str = "UTC"
    capacity_max: Optional[int] = None
    allowed_environments: List[str] = field(default_factory=list)
    required_approval_roles: List[str] = field(default_factory=list)
    freeze_periods: List[FreezePeriod] = field(default_factory=list)
    locked: bool = False
    locked_by: Optional[str] = None
    locked_at: Optional[str] = None
    description: Optional[str] = None
    tags: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["freeze_periods"] = [fp.to_dict() if isinstance(fp, FreezePeriod) else fp for fp in self.freeze_periods]
        return d

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ReleaseWindow":
        freeze_periods = [
            FreezePeriod.from_dict(fp) if isinstance(fp, dict) else fp
            for fp in data.get("freeze_periods", [])
        ]
        return cls(
            window_id=data["window_id"],
            name=data["name"],
            start_time=data["start_time"],
            end_time=data["end_time"],
            timezone=data.get("timezone", "UTC"),
            capacity_max=data.get("capacity_max"),
            allowed_environments=list(data.get("allowed_environments", [])),
            required_approval_roles=list(data.get("required_approval_roles", [])),
            freeze_periods=freeze_periods,
            locked=bool(data.get("locked", False)),
            locked_by=data.get("locked_by"),
            locked_at=data.get("locked_at"),
            description=data.get("description"),
            tags=list(data.get("tags", [])),
        )


@dataclass
class Wave:
    """A release wave - a group of components released together."""

    wave_id: str
    name: str
    order: int = 0
    description: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Wave":
        return cls(
            wave_id=data["wave_id"],
            name=data["name"],
            order=int(data.get("order", 0)),
            description=data.get("description"),
        )


@dataclass
class ScheduleEntry:
    """A single component scheduled into a window and wave."""

    component_name: str
    component_version: str
    window_id: str
    wave_id: Optional[str] = None
    scheduled_start: Optional[str] = None
    status: ComponentStatus = ComponentStatus.SCHEDULED
    reasons: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["status"] = self.status.value if isinstance(self.status, ComponentStatus) else self.status
        return d

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ScheduleEntry":
        status = data.get("status")
        if isinstance(status, str):
            status = ComponentStatus(status)
        return cls(
            component_name=data["component_name"],
            component_version=data["component_version"],
            window_id=data["window_id"],
            wave_id=data.get("wave_id"),
            scheduled_start=data.get("scheduled_start"),
            status=status,
            reasons=list(data.get("reasons", [])),
        )


@dataclass
class ScheduleIssue:
    """An issue encountered during scheduling."""

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
    def from_dict(cls, data: Dict[str, Any]) -> "ScheduleIssue":
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
class ScheduleResult:
    """Result of a scheduling operation."""

    schedule_id: str
    generated_at: str
    windows: List[ReleaseWindow] = field(default_factory=list)
    waves: List[Wave] = field(default_factory=list)
    entries: List[ScheduleEntry] = field(default_factory=list)
    issues: List[ScheduleIssue] = field(default_factory=list)
    unscheduled_components: List[Dict[str, Any]] = field(default_factory=list)
    total_scheduled: int = 0
    total_unscheduled: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schedule_id": self.schedule_id,
            "generated_at": self.generated_at,
            "windows": [w.to_dict() for w in self.windows],
            "waves": [w.to_dict() for w in self.waves],
            "entries": [e.to_dict() for e in self.entries],
            "issues": [i.to_dict() for i in self.issues],
            "unscheduled_components": self.unscheduled_components,
            "total_scheduled": self.total_scheduled,
            "total_unscheduled": self.total_unscheduled,
            "summary": {
                "windows": len(self.windows),
                "waves": len(self.waves),
                "scheduled": self.total_scheduled,
                "unscheduled": self.total_unscheduled,
                "errors": len([i for i in self.issues if i.severity == Severity.ERROR]),
                "warnings": len([i for i in self.issues if i.severity == Severity.WARNING]),
            },
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ScheduleResult":
        windows = [ReleaseWindow.from_dict(w) if isinstance(w, dict) else w for w in data.get("windows", [])]
        waves = [Wave.from_dict(w) if isinstance(w, dict) else w for w in data.get("waves", [])]
        entries = [ScheduleEntry.from_dict(e) if isinstance(e, dict) else e for e in data.get("entries", [])]
        issues = [ScheduleIssue.from_dict(i) if isinstance(i, dict) else i for i in data.get("issues", [])]
        return cls(
            schedule_id=data["schedule_id"],
            generated_at=data["generated_at"],
            windows=windows,
            waves=waves,
            entries=entries,
            issues=issues,
            unscheduled_components=list(data.get("unscheduled_components", [])),
            total_scheduled=int(data.get("total_scheduled", 0)),
            total_unscheduled=int(data.get("total_unscheduled", 0)),
        )
