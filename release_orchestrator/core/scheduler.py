"""Release scheduling engine - windows, waves, and constraints.

Takes a validated manifest plus release window/wave configuration
and schedules components into windows based on:
- Dependencies (dependencies must be scheduled in earlier or same wave)
- Environment constraints (window allowed_environments)
- Approval status (window required_approval_roles)
- Window capacity (capacity_max)
- Freeze periods (cannot schedule during freeze)
- Window locks (locked windows cannot accept new components)
- Policy environment rules
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Set, Tuple

from .models import (
    ApprovalStatus,
    Component,
    ComponentStatus,
    EnvironmentType,
    FreezePeriod,
    ReleaseManifest,
    ReleaseWindow,
    ScheduleEntry,
    ScheduleIssue,
    ScheduleResult,
    Severity,
    Wave,
    generate_id,
    now_iso,
)
from .policy import ReleasePolicy, default_policy
from .validator import ValidationEngine
from ..utils.logger import get_logger
from ..utils.storage import DEFAULT_WORK_DIR, get_work_dir, load_json, save_json
from ..utils.exit_codes import (
    EXIT_APPROVAL_MISSING,
    EXIT_CONFIG_ERROR,
    EXIT_FILE_NOT_FOUND,
    EXIT_PLAN_ERROR,
    EXIT_SCHEDULE_ERROR,
    EXIT_WINDOW_FROZEN,
    EXIT_WINDOW_LOCKED,
)

LOG = get_logger()
MODULE = "scheduler"


def _parse_iso_dt(s: str) -> datetime:
    """Parse ISO-8601 datetime string to UTC datetime."""
    s = s.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        raise ValueError(f"Invalid ISO-8601 datetime: {s}")
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


class SchedulingEngine:
    """Schedules components into release windows and waves."""

    def __init__(
        self,
        manifest: ReleaseManifest,
        windows: List[ReleaseWindow],
        waves: Optional[List[Wave]] = None,
        policy: Optional[ReleasePolicy] = None,
        validation: Optional[Any] = None,
    ):
        self.manifest = manifest
        self.windows: List[ReleaseWindow] = sorted(
            windows, key=lambda w: _parse_iso_dt(w.start_time)
        )
        self.waves: List[Wave] = sorted(
            waves or [], key=lambda w: w.order
        )
        self.policy: ReleasePolicy = policy if policy is not None else default_policy()
        self.validation = validation
        self.issues: List[ScheduleIssue] = []
        self._components_by_name: Dict[str, Component] = {
            c.name: c for c in manifest.components
        }
        self._windows_by_id: Dict[str, ReleaseWindow] = {
            w.window_id: w for w in self.windows
        }
        self._waves_by_id: Dict[str, Wave] = {
            w.wave_id: w for w in self.waves
        }
        self._window_usage: Dict[str, int] = defaultdict(int)
        self._current_entries: List[ScheduleEntry] = []

    def add_issue(
        self,
        severity: Severity,
        code: str,
        message: str,
        component: Optional[str] = None,
        details: Optional[Dict[str, Any]] = None,
    ) -> None:
        issue = ScheduleIssue(
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
        getattr(LOG, level)(
            MODULE, f"[{code}] {message}", component=component or "-"
        )

    def schedule(self) -> ScheduleResult:
        """Run the full scheduling process."""
        LOG.info(
            MODULE,
            "Starting scheduling",
            release_id=self.manifest.release_id,
            windows=len(self.windows),
            waves=len(self.waves),
        )

        self._validate_configuration()
        if self._has_errors():
            return self._build_result([])

        entries: List[ScheduleEntry] = []
        order = self._topological_order()

        for comp_name in order:
            comp = self._components_by_name[comp_name]
            entry = self._schedule_component(comp)
            if entry:
                entries.append(entry)
                self._current_entries.append(entry)

        unscheduled: List[Dict[str, Any]] = []
        for comp in self.manifest.components:
            if comp.name not in {e.component_name for e in entries}:
                reasons = [
                    i.message
                    for i in self.issues
                    if i.component == comp.name
                ]
                unscheduled.append({
                    "component": comp.name,
                    "version": comp.version,
                    "reasons": reasons or ["No matching window available"],
                })

        result = self._build_result(entries, unscheduled)
        LOG.info(
            MODULE,
            "Scheduling complete",
            schedule_id=result.schedule_id,
            scheduled=result.total_scheduled,
            unscheduled=result.total_unscheduled,
            errors=len([i for i in result.issues if i.severity == Severity.ERROR]),
            warnings=len([i for i in result.issues if i.severity == Severity.WARNING]),
        )
        return result

    def lock_window(self, window_id: str, locked_by: str) -> bool:
        """Lock a window to prevent further scheduling."""
        window = self._windows_by_id.get(window_id)
        if not window:
            self.add_issue(
                Severity.ERROR,
                "WINDOW_NOT_FOUND",
                f"Window not found: {window_id}",
                details={"window_id": window_id},
            )
            return False
        if window.locked:
            self.add_issue(
                Severity.WARNING,
                "WINDOW_ALREADY_LOCKED",
                f"Window {window_id} is already locked",
                details={"window_id": window_id},
            )
            return False
        window.locked = True
        window.locked_by = locked_by
        window.locked_at = now_iso()
        LOG.info(
            MODULE,
            "Window locked",
            window_id=window_id,
            locked_by=locked_by,
        )
        return True

    def unlock_window(self, window_id: str, unlocked_by: str) -> bool:
        """Unlock a window to allow scheduling."""
        window = self._windows_by_id.get(window_id)
        if not window:
            self.add_issue(
                Severity.ERROR,
                "WINDOW_NOT_FOUND",
                f"Window not found: {window_id}",
                details={"window_id": window_id},
            )
            return False
        if not window.locked:
            self.add_issue(
                Severity.WARNING,
                "WINDOW_ALREADY_UNLOCKED",
                f"Window {window_id} is already unlocked",
                details={"window_id": window_id},
            )
            return False
        window.locked = False
        window.locked_by = None
        window.locked_at = None
        LOG.info(
            MODULE,
            "Window unlocked",
            window_id=window_id,
            unlocked_by=unlocked_by,
        )
        return True

    def _validate_configuration(self) -> None:
        """Validate windows and waves configuration."""
        if not self.windows:
            self.add_issue(
                Severity.ERROR,
                "NO_WINDOWS",
                "No release windows configured for scheduling",
            )
            return

        seen_ids: Set[str] = set()
        for w in self.windows:
            if w.window_id in seen_ids:
                self.add_issue(
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
                    self.add_issue(
                        Severity.ERROR,
                        "INVALID_WINDOW_TIME",
                        f"Window {w.name}: start_time must be before end_time",
                        details={"window_id": w.window_id, "start": w.start_time, "end": w.end_time},
                    )
            except ValueError as e:
                self.add_issue(
                    Severity.ERROR,
                    "INVALID_DATETIME",
                    f"Window {w.name}: {e}",
                    details={"window_id": w.window_id},
                )

            for fp in w.freeze_periods:
                try:
                    fp_start = _parse_iso_dt(fp.start)
                    fp_end = _parse_iso_dt(fp.end)
                    if fp_start >= fp_end:
                        self.add_issue(
                            Severity.ERROR,
                            "INVALID_FREEZE_PERIOD",
                            f"Freeze period {fp.name}: start must be before end",
                            details={"window_id": w.window_id, "freeze": fp.name},
                        )
                except ValueError as e:
                    self.add_issue(
                        Severity.ERROR,
                        "INVALID_DATETIME",
                        f"Freeze period {fp.name}: {e}",
                        details={"window_id": w.window_id, "freeze": fp.name},
                    )

        seen_wave_ids: Set[str] = set()
        for wave in self.waves:
            if wave.wave_id in seen_wave_ids:
                self.add_issue(
                    Severity.ERROR,
                    "DUPLICATE_WAVE_ID",
                    f"Duplicate wave_id: {wave.wave_id}",
                    details={"wave_id": wave.wave_id},
                )
            seen_wave_ids.add(wave.wave_id)

    def _has_errors(self) -> bool:
        return any(i.severity == Severity.ERROR for i in self.issues)

    def _topological_order(self) -> List[str]:
        """Topologically sort components by dependencies."""
        graph: Dict[str, List[str]] = defaultdict(list)
        indegree: Dict[str, int] = {c.name: 0 for c in self.manifest.components}
        for c in self.manifest.components:
            for dep in c.dependencies:
                if dep.required and dep.name in indegree:
                    graph[dep.name].append(c.name)
                    indegree[c.name] += 1

        from collections import deque
        q = deque([n for n, d in indegree.items() if d == 0])
        result: List[str] = []
        while q:
            node = q.popleft()
            result.append(node)
            for nxt in graph[node]:
                indegree[nxt] -= 1
                if indegree[nxt] == 0:
                    q.append(nxt)

        for c in self.manifest.components:
            if c.name not in result:
                result.append(c.name)
        return result

    def _schedule_component(self, comp: Component) -> Optional[ScheduleEntry]:
        """Try to schedule a single component into the best matching window."""
        reasons: List[str] = []

        for window in self.windows:
            can_schedule, reason = self._check_window(comp, window)
            if not can_schedule:
                reasons.append(f"Window {window.name}: {reason}")
                continue

            wave = self._select_wave(comp, window)
            entry = ScheduleEntry(
                component_name=comp.name,
                component_version=comp.version,
                window_id=window.window_id,
                wave_id=wave.wave_id if wave else None,
                scheduled_start=window.start_time,
                status=ComponentStatus.SCHEDULED,
                reasons=[f"Scheduled into {window.name}" + (f" wave {wave.name}" if wave else "")],
            )
            self._window_usage[window.window_id] += 1
            self.add_issue(
                Severity.INFO,
                "COMPONENT_SCHEDULED",
                f"Scheduled {comp.name} v{comp.version} into {window.name}" + (f" wave {wave.name}" if wave else ""),
                component=comp.name,
                details={"window_id": window.window_id, "wave_id": wave.wave_id if wave else None},
            )
            return entry

        for reason in reasons:
            self.add_issue(
                Severity.ERROR,
                "SCHEDULE_FAILED",
                reason,
                component=comp.name,
            )
        return None

    def _check_window(self, comp: Component, window: ReleaseWindow) -> Tuple[bool, str]:
        """Check if a component can be scheduled into a window."""
        if window.locked:
            return False, "Window is locked"

        env_name = comp.environment.value if isinstance(comp.environment, EnvironmentType) else str(comp.environment)
        if window.allowed_environments and env_name not in window.allowed_environments:
            return False, f"Environment '{env_name}' not allowed (allowed: {', '.join(window.allowed_environments)})"

        env_policy = self.policy.get_env_policy(env_name)
        if env_policy.require_approval:
            has_approved = any(a.status == ApprovalStatus.APPROVED for a in comp.approvals)
            if not has_approved:
                return False, "Missing required approval for environment"

            if window.required_approval_roles:
                approvers = {a.approver for a in comp.approvals if a.status == ApprovalStatus.APPROVED}
                role_match = any(role in approvers for role in window.required_approval_roles)
                if not role_match:
                    return False, f"Approval by one of {', '.join(window.required_approval_roles)} required"

        window_start = _parse_iso_dt(window.start_time)
        window_end = _parse_iso_dt(window.end_time)
        scheduled_time = window_start
        for fp in window.freeze_periods:
            fp_start = _parse_iso_dt(fp.start)
            fp_end = _parse_iso_dt(fp.end)
            if fp_start <= scheduled_time < fp_end:
                return False, f"Scheduled time overlaps with freeze period '{fp.name}'"

        if window.capacity_max is not None:
            current = self._window_usage.get(window.window_id, 0)
            if current >= window.capacity_max:
                return False, f"Capacity full ({current}/{window.capacity_max})"

        prereqs = [d.name for d in comp.dependencies if d.required and d.name in self._components_by_name]
        for prereq in prereqs:
            prereq_comp = self._components_by_name[prereq]
            prereq_window = self._find_scheduled_window(prereq)
            if prereq_window is None:
                continue
            prereq_win = self._windows_by_id.get(prereq_window)
            cur_win_start = _parse_iso_dt(window.start_time)
            prereq_win_start = _parse_iso_dt(prereq_win.start_time) if prereq_win else cur_win_start
            if cur_win_start < prereq_win_start:
                return False, f"Dependency {prereq} is scheduled in later window {prereq_win.name if prereq_win else 'unknown'}"

        return True, ""

    def _select_wave(self, comp: Component, window: ReleaseWindow) -> Optional[Wave]:
        """Select the appropriate wave for a component within a window."""
        if not self.waves:
            return None

        prereqs = [d.name for d in comp.dependencies if d.required and d.name in self._components_by_name]
        if not prereqs:
            return self.waves[0] if self.waves else None

        prereq_wave_orders: List[int] = []
        for p in prereqs:
            pw = self._find_scheduled_wave(p)
            if pw is not None:
                wave = self._waves_by_id.get(pw)
                if wave:
                    prereq_wave_orders.append(wave.order)

        min_order = max(prereq_wave_orders) if prereq_wave_orders else 0
        for wave in self.waves:
            if wave.order > min_order:
                return wave

        return self.waves[-1] if self.waves else None

    def _find_scheduled_window(self, component_name: str) -> Optional[str]:
        for e in self._current_entries:
            if e.component_name == component_name:
                return e.window_id
        return None

    def _find_scheduled_wave(self, component_name: str) -> Optional[str]:
        for e in self._current_entries:
            if e.component_name == component_name:
                return e.wave_id
        return None

    def _build_result(
        self,
        entries: List[ScheduleEntry],
        unscheduled: Optional[List[Dict[str, Any]]] = None,
    ) -> ScheduleResult:
        unscheduled = unscheduled or []
        return ScheduleResult(
            schedule_id=generate_id("SCHED"),
            generated_at=now_iso(),
            windows=self.windows,
            waves=self.waves,
            entries=entries,
            issues=self.issues,
            unscheduled_components=unscheduled,
            total_scheduled=len(entries),
            total_unscheduled=len(unscheduled),
        )

    def determine_exit_code(self) -> int:
        codes = {i.issue_code for i in self.issues if i.severity == Severity.ERROR}
        if "WINDOW_LOCKED" in codes or "WINDOW_ALREADY_LOCKED" in codes:
            return EXIT_WINDOW_LOCKED.code
        if "INVALID_FREEZE_PERIOD" in codes or "FREEZE_PERIOD" in str(codes):
            return EXIT_WINDOW_FROZEN.code
        if "APPROVAL_MISSING" in codes or "SCHEDULE_FAILED" in codes:
            has_approval = any("approval" in i.message.lower() for i in self.issues if i.severity == Severity.ERROR)
            if has_approval:
                return EXIT_APPROVAL_MISSING.code
            return EXIT_SCHEDULE_ERROR.code
        if "NO_WINDOWS" in codes or "DUPLICATE" in codes or "INVALID" in codes:
            return EXIT_CONFIG_ERROR.code
        if self._has_errors():
            return EXIT_SCHEDULE_ERROR.code
        return 0


def load_windows_from_json(path: str) -> List[ReleaseWindow]:
    """Load release windows from a JSON file."""
    import json
    from pathlib import Path

    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"Windows configuration file not found: {path}")
    try:
        with p.open("r", encoding="utf-8") as f:
            data = json.load(f)
    except json.JSONDecodeError as e:
        raise ValueError(f"Invalid windows JSON in '{p}': {e}")

    if isinstance(data, list):
        windows_data = data
    else:
        windows_data = data.get("windows", [])
    windows = []
    for idx, w in enumerate(windows_data):
        if "window_id" not in w:
            w["window_id"] = generate_id("WIN")
        try:
            windows.append(ReleaseWindow.from_dict(w))
        except ValueError as exc:
            raise ValueError(
                f"Invalid window at index {idx} in '{p}': {exc}"
            ) from exc

    LOG.info(
        MODULE,
        f"Loaded {len(windows)} windows from JSON",
        path=str(p),
    )
    return windows


def load_windows_from_csv(path: str) -> List[ReleaseWindow]:
    """Load release windows from a CSV file."""
    import csv
    from pathlib import Path

    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"Windows configuration file not found: {path}")

    windows: List[ReleaseWindow] = []
    try:
        with p.open("r", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            for row in reader:
                allowed = [s.strip() for s in row.get("allowed_environments", "").split(",") if s.strip()]
                roles = [s.strip() for s in row.get("required_approval_roles", "").split(",") if s.strip()]
                tags = [s.strip() for s in row.get("tags", "").split(",") if s.strip()]

                freeze_periods: List[FreezePeriod] = []
                freeze_names = row.get("freeze_periods", "")
                if freeze_names:
                    for name_part in freeze_names.split(";"):
                        name_part = name_part.strip()
                        if name_part:
                            parts = name_part.split("|")
                            if len(parts) >= 3:
                                freeze_periods.append(FreezePeriod(
                                    name=parts[0].strip(),
                                    start=parts[1].strip(),
                                    end=parts[2].strip(),
                                    reason=parts[3].strip() if len(parts) > 3 else None,
                                ))

                capacity = row.get("capacity_max")
                capacity_max = int(capacity) if capacity and capacity.strip() else None

                window = ReleaseWindow(
                    window_id=row.get("window_id") or generate_id("WIN"),
                    name=row.get("name", "Unnamed Window"),
                    start_time=row["start_time"],
                    end_time=row["end_time"],
                    timezone=row.get("timezone", "UTC"),
                    capacity_max=capacity_max,
                    allowed_environments=allowed,
                    required_approval_roles=roles,
                    freeze_periods=freeze_periods,
                    locked=row.get("locked", "").lower() in ("true", "1", "yes"),
                    locked_by=row.get("locked_by") or None,
                    locked_at=row.get("locked_at") or None,
                    description=row.get("description") or None,
                    tags=tags,
                )
                windows.append(window)
    except (csv.Error, KeyError) as e:
        raise ValueError(f"Invalid windows CSV: {e}")

    LOG.info(
        MODULE,
        f"Loaded {len(windows)} windows from CSV",
        path=str(p),
    )
    return windows


def load_waves_from_json(path: str) -> List[Wave]:
    """Load waves from a JSON file."""
    import json
    from pathlib import Path

    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"Waves configuration file not found: {path}")
    try:
        with p.open("r", encoding="utf-8") as f:
            data = json.load(f)
    except json.JSONDecodeError as e:
        raise ValueError(f"Invalid waves JSON in '{p}': {e}")

    waves_data = data.get("waves", data if isinstance(data, list) else [])
    waves = []
    for idx, w in enumerate(waves_data):
        if "wave_id" not in w:
            w["wave_id"] = generate_id("WAVE")
        try:
            waves.append(Wave.from_dict(w))
        except ValueError as exc:
            raise ValueError(
                f"Invalid wave at index {idx} in '{p}': {exc}"
            ) from exc

    LOG.info(
        MODULE,
        f"Loaded {len(waves)} waves from JSON",
        path=str(p),
    )
    return waves


WINDOW_STATE_FILE = "window_state.json"


def save_window_state(windows: List[ReleaseWindow], base: Optional[str] = None) -> None:
    """Persist window state (locked/locked_by/locked_at) to work dir."""
    work = get_work_dir(base)
    state = {
        w.window_id: {
            "locked": w.locked,
            "locked_by": w.locked_by,
            "locked_at": w.locked_at,
        }
        for w in windows
    }
    save_json(state, work / WINDOW_STATE_FILE)
    LOG.info(MODULE, "Saved window state", file=str(work / WINDOW_STATE_FILE), windows=len(state))


def load_window_state(windows: List[ReleaseWindow], base: Optional[str] = None) -> int:
    """Merge persisted window state into the provided windows list.

    Returns the number of windows that had state applied.
    """
    work = get_work_dir(base)
    state_file = work / WINDOW_STATE_FILE
    if not state_file.exists():
        return 0

    try:
        state = load_json(state_file)
    except Exception:
        return 0

    applied = 0
    by_id = {w.window_id: w for w in windows}
    for window_id, win_state in state.items():
        w = by_id.get(window_id)
        if w:
            w.locked = win_state.get("locked", False)
            w.locked_by = win_state.get("locked_by")
            w.locked_at = win_state.get("locked_at")
            applied += 1

    if applied > 0:
        LOG.info(MODULE, "Loaded window state from disk", applied=applied, file=str(state_file))
    return applied


def validate_and_schedule(
    manifest_path: str,
    windows_path: Optional[str] = None,
    waves_path: Optional[str] = None,
    policy_path: Optional[str] = None,
) -> Tuple[Optional[ScheduleResult], Optional[Any], int]:
    """Convenience helper: load, validate, and schedule."""
    from ..utils.storage import load_manifest
    from ..utils.policy_loader import load_policy

    try:
        manifest = load_manifest(manifest_path)
    except FileNotFoundError:
        LOG.error(MODULE, f"Manifest not found: {manifest_path}")
        return None, None, EXIT_PLAN_ERROR.code
    except ValueError as exc:
        LOG.error(MODULE, f"Invalid manifest: {exc}")
        return None, None, EXIT_PLAN_ERROR.code

    try:
        policy = load_policy(policy_path)
    except FileNotFoundError:
        policy = default_policy()
    except Exception as exc:
        LOG.error(MODULE, f"Failed to load policy: {exc}")
        return None, None, EXIT_CONFIG_ERROR.code

    windows: List[ReleaseWindow] = []
    waves: Optional[List[Wave]] = None

    if windows_path:
        try:
            if windows_path.lower().endswith(".csv"):
                windows = load_windows_from_csv(windows_path)
            else:
                windows = load_windows_from_json(windows_path)
        except FileNotFoundError:
            LOG.error(MODULE, f"Windows config not found: {windows_path}")
            return None, None, EXIT_FILE_NOT_FOUND.code
        except ValueError as exc:
            LOG.error(MODULE, f"Invalid windows config: {exc}")
            return None, None, EXIT_CONFIG_ERROR.code

    manifest_windows = getattr(manifest, "release_windows", None)
    if manifest_windows and not windows:
        windows = [
            ReleaseWindow.from_dict(w) if isinstance(w, dict) else w
            for w in manifest_windows
        ]
        LOG.info(MODULE, f"Loaded {len(windows)} windows from manifest")

    manifest_waves = getattr(manifest, "waves", None)
    if manifest_waves:
        waves = [
            Wave.from_dict(w) if isinstance(w, dict) else w
            for w in manifest_waves
        ]
        LOG.info(MODULE, f"Loaded {len(waves)} waves from manifest")

    if waves_path:
        try:
            waves = load_waves_from_json(waves_path)
        except FileNotFoundError:
            LOG.error(MODULE, f"Waves config not found: {waves_path}")
            return None, None, EXIT_FILE_NOT_FOUND.code
        except ValueError as exc:
            LOG.error(MODULE, f"Invalid waves config: {exc}")
            return None, None, EXIT_CONFIG_ERROR.code

    if not windows:
        LOG.error(MODULE, "No windows configured for scheduling")
        return None, None, EXIT_CONFIG_ERROR.code

    load_window_state(windows, base=None)

    engine = ValidationEngine(manifest, policy=policy)
    validation = engine.validate()
    exit_code = engine.determine_exit_code()
    if exit_code != 0:
        LOG.warning(MODULE, f"Validation failed (exit={exit_code}), still attempting scheduling")

    scheduler = SchedulingEngine(manifest, windows, waves, policy, validation)
    try:
        result = scheduler.schedule()
    except Exception as exc:
        LOG.error(MODULE, f"Scheduling failed: {exc}")
        return None, validation, EXIT_SCHEDULE_ERROR.code

    schedule_exit = scheduler.determine_exit_code()
    final_exit = exit_code if exit_code != 0 else schedule_exit
    return result, validation, final_exit
