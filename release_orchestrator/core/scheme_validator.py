"""Validation utilities for release scheduling schemes.

Checks that saved schemes have compatible manifest structure,
no window conflicts, no reuse of locked windows, and valid JSON.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from .models import (
    ReleaseManifest,
    ReleaseScheme,
    ReleaseWindow,
    Severity,
    Wave,
    now_iso,
)
from ..utils.logger import get_logger

LOG = get_logger()
MODULE = "scheme_validator"


def _parse_iso_dt(s: str) -> datetime:
    s = s.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        raise ValueError(f"Invalid ISO-8601 datetime: {s}")
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


class SchemeValidationError(Exception):
    """Raised when a scheme fails validation."""

    def __init__(self, issues: List[Dict[str, Any]]):
        self.issues = issues
        super().__init__(
            "Scheme validation failed with "
            f"{len([i for i in issues if i.get('severity') == 'error'])} error(s): "
            + "; ".join(
                i["message"]
                for i in issues
                if i.get("severity") == "error"
            )
        )


class SchemeValidator:
    """Validates ReleaseScheme instances for structural correctness and
    compatibility with existing window locks/snapshots.
    """

    def __init__(self, scheme: ReleaseScheme, existing_locks: Optional[Dict[str, Any]] = None):
        self.scheme = scheme
        self.existing_locks = existing_locks or {}
        self.issues: List[Dict[str, Any]] = []

    def _add_issue(
        self,
        severity: Severity,
        code: str,
        message: str,
        details: Optional[Dict[str, Any]] = None,
    ) -> None:
        self.issues.append({
            "severity": severity.value,
            "issue_code": code,
            "message": message,
            "details": details,
        })
        level = {
            Severity.ERROR: "error",
            Severity.WARNING: "warning",
            Severity.INFO: "info",
        }[severity]
        getattr(LOG, level)(MODULE, f"[{code}] {message}")

    def validate(self) -> List[Dict[str, Any]]:
        """Run all validation checks and return the list of issues."""
        LOG.info(MODULE, "Validating scheme", scheme_name=self.scheme.scheme_name)
        self._validate_required_fields()
        self._validate_manifest_structure()
        self._validate_windows()
        self._validate_window_conflicts()
        self._validate_locked_window_reuse()
        self._validate_waves()
        return self.issues

    def validate_and_raise(self) -> None:
        """Run validation and raise SchemeValidationError on errors."""
        issues = self.validate()
        errors = [i for i in issues if i.get("severity") == "error"]
        if errors:
            raise SchemeValidationError(issues)

    def _validate_required_fields(self) -> None:
        s = self.scheme
        if not s.scheme_name or not isinstance(s.scheme_name, str) or not s.scheme_name.strip():
            self._add_issue(
                Severity.ERROR,
                "SCHEME_NAME_MISSING",
                "Scheme name is required and must be a non-empty string",
            )
        if not s.created_at:
            self._add_issue(
                Severity.ERROR,
                "CREATED_AT_MISSING",
                "Scheme created_at timestamp is required",
            )
        if not s.created_by:
            self._add_issue(
                Severity.WARNING,
                "CREATED_BY_MISSING",
                "Scheme created_by is not set; defaulting to unknown",
            )
        if not s.release_windows and not s.manifest:
            self._add_issue(
                Severity.ERROR,
                "NO_WINDOWS_OR_MANIFEST",
                "Scheme must include either release_windows or a manifest with embedded windows",
            )

    def _validate_manifest_structure(self) -> None:
        manifest = self.scheme.manifest
        if manifest is None:
            return
        if not isinstance(manifest, dict):
            self._add_issue(
                Severity.ERROR,
                "MANIFEST_INVALID_TYPE",
                "Scheme manifest must be a dict or None",
                details={"type": type(manifest).__name__},
            )
            return
        required_manifest_fields = [
            "manifest_version",
            "release_id",
            "title",
            "target_environment",
        ]
        for field in required_manifest_fields:
            if field not in manifest:
                self._add_issue(
                    Severity.ERROR,
                    f"MANIFEST_MISSING_{field.upper()}",
                    f"Manifest is missing required field: {field}",
                    details={"field": field},
                )
        if manifest.get("components") is not None:
            if not isinstance(manifest["components"], list):
                self._add_issue(
                    Severity.ERROR,
                    "MANIFEST_COMPONENTS_INVALID",
                    "Manifest components must be a list",
                )

    def _validate_windows(self) -> None:
        windows = self.scheme.release_windows or []
        seen_ids = set()
        for w in windows:
            if not isinstance(w, ReleaseWindow):
                self._add_issue(
                    Severity.ERROR,
                    "WINDOW_INVALID_TYPE",
                    f"Each release window must be a ReleaseWindow instance, got {type(w).__name__}",
                )
                continue
            if not w.window_id:
                self._add_issue(
                    Severity.ERROR,
                    "WINDOW_ID_MISSING",
                    "Release window is missing window_id",
                    details={"window_name": getattr(w, "name", "?")},
                )
                continue
            if w.window_id in seen_ids:
                self._add_issue(
                    Severity.ERROR,
                    "DUPLICATE_WINDOW_ID",
                    f"Duplicate window_id: {w.window_id}",
                    details={"window_id": w.window_id},
                )
            seen_ids.add(w.window_id)

            try:
                start = _parse_iso_dt(w.start_time)
                end = _parse_iso_dt(w.end_time)
                if start >= end:
                    self._add_issue(
                        Severity.ERROR,
                        "INVALID_WINDOW_TIME",
                        f"Window '{w.name}': start_time must be before end_time",
                        details={"window_id": w.window_id, "start": w.start_time, "end": w.end_time},
                    )
            except ValueError as e:
                self._add_issue(
                    Severity.ERROR,
                    "INVALID_DATETIME",
                    f"Window '{w.name}': {e}",
                    details={"window_id": w.window_id},
                )

            for fp in w.freeze_periods:
                try:
                    fp_start = _parse_iso_dt(fp.start)
                    fp_end = _parse_iso_dt(fp.end)
                    if fp_start >= fp_end:
                        self._add_issue(
                            Severity.ERROR,
                            "INVALID_FREEZE_PERIOD",
                            f"Freeze period '{fp.name}' in window '{w.name}': start must be before end",
                            details={"window_id": w.window_id, "freeze": fp.name},
                        )
                except ValueError as e:
                    self._add_issue(
                        Severity.ERROR,
                        "INVALID_DATETIME",
                        f"Freeze period '{fp.name}' in window '{w.name}': {e}",
                        details={"window_id": w.window_id, "freeze": fp.name},
                    )

    def _validate_window_conflicts(self) -> None:
        """Check that windows don't have overlapping time ranges with the
        same capacity constraints that could cause double-booking confusion.
        """
        windows = [w for w in (self.scheme.release_windows or []) if isinstance(w, ReleaseWindow)]
        for i, w1 in enumerate(windows):
            try:
                w1_start = _parse_iso_dt(w1.start_time)
                w1_end = _parse_iso_dt(w1.end_time)
            except ValueError:
                continue
            for w2 in windows[i + 1:]:
                try:
                    w2_start = _parse_iso_dt(w2.start_time)
                    w2_end = _parse_iso_dt(w2.end_time)
                except ValueError:
                    continue
                if w1_start < w2_end and w2_start < w1_end:
                    envs1 = set(w1.allowed_environments)
                    envs2 = set(w2.allowed_environments)
                    shared_envs = envs1 & envs2
                    if shared_envs or (not envs1 and not envs2):
                        self._add_issue(
                            Severity.WARNING,
                            "WINDOW_TIME_OVERLAP",
                            f"Windows '{w1.name}' and '{w2.name}' have overlapping time ranges",
                            details={
                                "window_a": w1.window_id,
                                "window_b": w2.window_id,
                                "overlap_envs": sorted(shared_envs) if shared_envs else "(all environments)",
                            },
                        )

    def _validate_locked_window_reuse(self) -> None:
        """Check that windows marked as locked (persisted via window_state)
        are not being reused for scheduling in a scheme unless explicitly
        unlocked.
        """
        if not self.existing_locks:
            return
        windows = self.scheme.release_windows or []
        for w in windows:
            if not isinstance(w, ReleaseWindow):
                continue
            lock_state = self.existing_locks.get(w.window_id)
            if lock_state and lock_state.get("locked"):
                if not w.locked:
                    # The persisted state says locked but the scheme-internal
                    # window says unlocked — flag as error to prevent reuse.
                    self._add_issue(
                        Severity.ERROR,
                        "LOCKED_WINDOW_REUSE",
                        f"Window '{w.name}' ({w.window_id}) is currently locked "
                        f"(by {lock_state.get('locked_by', 'unknown')} at "
                        f"{lock_state.get('locked_at', 'unknown')}) and cannot be scheduled into. "
                        f"Unlock it first via `schedule --unlock {w.window_id}`.",
                        details={
                            "window_id": w.window_id,
                            "locked_by": lock_state.get("locked_by"),
                            "locked_at": lock_state.get("locked_at"),
                        },
                    )
                else:
                    self._add_issue(
                        Severity.INFO,
                        "WINDOW_LOCKED",
                        f"Window '{w.name}' ({w.window_id}) is marked as locked in scheme",
                        details={"window_id": w.window_id},
                    )

    def _validate_waves(self) -> None:
        waves = self.scheme.waves or []
        seen_ids = set()
        for w in waves:
            if not isinstance(w, Wave):
                self._add_issue(
                    Severity.ERROR,
                    "WAVE_INVALID_TYPE",
                    f"Each wave must be a Wave instance, got {type(w).__name__}",
                )
                continue
            if not w.wave_id:
                self._add_issue(
                    Severity.ERROR,
                    "WAVE_ID_MISSING",
                    "Wave is missing wave_id",
                    details={"wave_name": getattr(w, "name", "?")},
                )
                continue
            if w.wave_id in seen_ids:
                self._add_issue(
                    Severity.ERROR,
                    "DUPLICATE_WAVE_ID",
                    f"Duplicate wave_id: {w.wave_id}",
                    details={"wave_id": w.wave_id},
                )
            seen_ids.add(w.wave_id)


def validate_scheme(
    scheme: ReleaseScheme,
    existing_locks: Optional[Dict[str, Any]] = None,
) -> Tuple[bool, List[Dict[str, Any]]]:
    """Validate a ReleaseScheme and return (passed, issues)."""
    validator = SchemeValidator(scheme, existing_locks)
    issues = validator.validate()
    errors = [i for i in issues if i.get("severity") == "error"]
    return (len(errors) == 0, issues)
