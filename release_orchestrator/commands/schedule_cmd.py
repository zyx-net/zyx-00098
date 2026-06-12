"""`schedule` command - schedule components into release windows and waves."""
from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

from .base import CommandResult
from ..core.models import ReleaseWindow, ScheduleResult
from ..core.scheduler import (
    SchedulingEngine,
    load_waves_from_json,
    load_windows_from_csv,
    load_windows_from_json,
    load_window_state,
    save_window_state,
    validate_and_schedule,
)
from ..core.validator import ValidationEngine
from ..utils.exit_codes import (
    EXIT_CONFIG_ERROR,
    EXIT_FILE_NOT_FOUND,
    EXIT_OK,
    EXIT_SCHEDULE_ERROR,
    EXIT_WINDOW_LOCKED,
)
from ..utils.logger import get_logger
from ..utils.policy_loader import PolicyValidationError, load_policy
from ..utils.storage import load_manifest, save_json

LOG = get_logger()
MODULE = "cmd.schedule"


def add_parser(subparsers: "argparse._SubParsersAction") -> None:
    p = subparsers.add_parser(
        "schedule",
        help="Schedule components into release windows and waves",
    )
    p.add_argument(
        "-m", "--manifest",
        default="examples/sample_manifest.json",
        help="Path to manifest JSON",
    )
    p.add_argument(
        "--policy", default=None,
        help="Path to release policy JSON file",
    )
    p.add_argument(
        "-w", "--windows", default=None,
        help="Path to windows configuration (JSON or CSV)",
    )
    p.add_argument(
        "--waves", default=None,
        help="Path to waves configuration JSON",
    )
    p.add_argument(
        "-o", "--output", default=None,
        help="Write schedule JSON to this file in addition to stdout",
    )
    p.add_argument(
        "--lock", default=None, metavar="WINDOW_ID",
        help="Lock a window (prevents scheduling into it)",
    )
    p.add_argument(
        "--unlock", default=None, metavar="WINDOW_ID",
        help="Unlock a window",
    )
    p.add_argument(
        "--by", default="admin@corp.com",
        help="User performing lock/unlock operation",
    )
    p.add_argument(
        "--skip-validate", action="store_true",
        help="Skip manifest validation before scheduling",
    )
    p.set_defaults(func=_run)


def _run(args: argparse.Namespace, **_: Any) -> CommandResult:
    try:
        manifest = load_manifest(args.manifest)
    except FileNotFoundError:
        LOG.error(MODULE, f"Manifest not found: {args.manifest}")
        return CommandResult(exit_code=EXIT_FILE_NOT_FOUND.code, run_id="")
    except ValueError as exc:
        LOG.error(MODULE, f"Invalid manifest: {exc}")
        return CommandResult(exit_code=EXIT_CONFIG_ERROR.code, run_id="")

    try:
        policy = load_policy(args.policy, work_dir=getattr(args, "work_dir", None))
    except FileNotFoundError as exc:
        LOG.error(MODULE, str(exc))
        print(f"ERROR: {exc}")
        return CommandResult(exit_code=EXIT_FILE_NOT_FOUND.code, run_id="")
    except PolicyValidationError as exc:
        LOG.error(MODULE, f"Invalid policy: {exc}")
        print(f"ERROR: Invalid policy: {exc}")
        for err in exc.errors:
            print(f"  - {err}")
        return CommandResult(exit_code=EXIT_CONFIG_ERROR.code, run_id="")

    windows = []
    waves = None

    if args.windows:
        try:
            if args.windows.lower().endswith(".csv"):
                windows = load_windows_from_csv(args.windows)
            else:
                windows = load_windows_from_json(args.windows)
        except FileNotFoundError:
            LOG.error(MODULE, f"Windows config not found: {args.windows}")
            return CommandResult(exit_code=EXIT_FILE_NOT_FOUND.code, run_id="")
        except ValueError as exc:
            LOG.error(MODULE, f"Invalid windows config: {exc}")
            print(f"ERROR: Invalid windows config: {exc}")
            return CommandResult(exit_code=EXIT_CONFIG_ERROR.code, run_id="")

    manifest_windows = getattr(manifest, "release_windows", None)
    if manifest_windows and not windows:
        from ..core.models import ReleaseWindow as RW
        windows = [RW.from_dict(w) if isinstance(w, dict) else w for w in manifest_windows]
        LOG.info(MODULE, f"Loaded {len(windows)} windows from manifest")

    manifest_waves = getattr(manifest, "waves", None)
    if manifest_waves:
        from ..core.models import Wave as W
        waves = [W.from_dict(w) if isinstance(w, dict) else w for w in manifest_waves]
        LOG.info(MODULE, f"Loaded {len(waves)} waves from manifest")

    if args.waves:
        try:
            waves = load_waves_from_json(args.waves)
        except FileNotFoundError:
            LOG.error(MODULE, f"Waves config not found: {args.waves}")
            return CommandResult(exit_code=EXIT_FILE_NOT_FOUND.code, run_id="")
        except ValueError as exc:
            LOG.error(MODULE, f"Invalid waves config: {exc}")
            return CommandResult(exit_code=EXIT_CONFIG_ERROR.code, run_id="")

    if windows:
        load_window_state(windows, base=getattr(args, "work_dir", None))

    policy_dict = policy.to_dict()

    if args.lock:
        return _handle_lock(args, manifest, windows, waves, policy, args.lock, args.by, True)
    if args.unlock:
        return _handle_lock(args, manifest, windows, waves, policy, args.unlock, args.by, False)

    if not windows:
        LOG.error(MODULE, "No windows configured for scheduling")
        print("ERROR: No windows configured. Use --windows or declare release_windows in manifest.")
        return CommandResult(exit_code=EXIT_CONFIG_ERROR.code, run_id="")

    validation_dict = None
    validation = None
    exit_code = EXIT_OK.code
    if not args.skip_validate:
        engine = ValidationEngine(manifest, policy=policy)
        validation = engine.validate()
        validation_dict = validation.to_dict()
        exit_code = engine.determine_exit_code()
    else:
        validation = None

    scheduler = SchedulingEngine(manifest, windows, waves, policy, validation)
    try:
        schedule = scheduler.schedule()
    except Exception as exc:
        LOG.error(MODULE, f"Scheduling failed: {exc}")
        return CommandResult(
            exit_code=EXIT_SCHEDULE_ERROR.code,
            run_id="",
            manifest_snapshot=manifest.to_dict(),
            validation_result=validation_dict,
            extra_artifacts={"policy_snapshot": policy_dict},
        )

    schedule_dict = schedule.to_dict()
    schedule_exit = scheduler.determine_exit_code()
    if exit_code == EXIT_OK.code:
        exit_code = schedule_exit

    _print_schedule(schedule)

    if args.output:
        save_json(schedule_dict, Path(args.output))
        print(f"\nSchedule written to: {args.output}")

    return CommandResult(
        exit_code=exit_code,
        run_id="",
        manifest_snapshot=manifest.to_dict(),
        validation_result=validation_dict,
        extra_artifacts={
            "policy_snapshot": policy_dict,
            "schedule_result": schedule_dict,
            "schedule_summary": _format_schedule_summary(schedule),
        },
    )


def _handle_lock(
    args: argparse.Namespace,
    manifest,
    windows,
    waves,
    policy,
    window_id: str,
    actor: str,
    lock: bool,
) -> CommandResult:
    """Handle lock/unlock operations."""
    if not windows:
        LOG.error(MODULE, "No windows loaded for lock operation")
        print("ERROR: No windows configured. Use --windows to load windows for lock/unlock.")
        return CommandResult(exit_code=EXIT_CONFIG_ERROR.code, run_id="")

    scheduler = SchedulingEngine(manifest, windows, waves, policy)
    success = False
    if lock:
        success = scheduler.lock_window(window_id, actor)
        action = "locked"
    else:
        success = scheduler.unlock_window(window_id, actor)
        action = "unlocked"

    exit_code = EXIT_OK.code if success else EXIT_WINDOW_LOCKED.code
    if success:
        save_window_state(windows, base=getattr(args, "work_dir", None))
        result = ScheduleResult(
            schedule_id="",
            generated_at="",
            windows=scheduler.windows,
            waves=scheduler.waves,
            entries=[],
            issues=scheduler.issues,
            unscheduled_components=[],
            total_scheduled=0,
            total_unscheduled=0,
        )
        schedule_dict = result.to_dict()
        print(f"\nWindow {window_id} {action} by {actor}")
        _print_windows(scheduler.windows)

        if args.output:
            save_json(schedule_dict, Path(args.output))
            print(f"\nWindow state written to: {args.output}")

        return CommandResult(
            exit_code=exit_code,
            run_id="",
            manifest_snapshot=manifest.to_dict(),
            extra_artifacts={
                "policy_snapshot": policy.to_dict(),
                "schedule_result": schedule_dict,
            },
        )
    else:
        print(f"\nERROR: Failed to {action} window {window_id}")
        for issue in scheduler.issues:
            print(f"  - [{issue.severity.value}] {issue.issue_code}: {issue.message}")
        return CommandResult(
            exit_code=exit_code,
            run_id="",
            manifest_snapshot=manifest.to_dict(),
            extra_artifacts={"policy_snapshot": policy.to_dict()},
        )


def _print_schedule(schedule: ScheduleResult) -> None:
    print(f"\n=== Release Schedule: {schedule.schedule_id} ===")
    print(f"Generated At        : {schedule.generated_at}")
    print(f"Windows             : {len(schedule.windows)}")
    print(f"Waves               : {len(schedule.waves)}")
    print(f"Scheduled           : {schedule.total_scheduled}")
    print(f"Unscheduled         : {schedule.total_unscheduled}")
    summary = schedule.to_dict().get("summary", {})
    print(f"Errors              : {summary.get('errors', 0)}")
    print(f"Warnings            : {summary.get('warnings', 0)}")

    _print_windows(schedule.windows)

    if schedule.waves:
        print(f"\nWaves:")
        for wave in schedule.waves:
            print(f"  - [{wave.order}] {wave.name} ({wave.wave_id})")

    print(f"\n{'Component':25s} {'Ver':10s} {'Window':20s} {'Wave':15s} {'Scheduled At':25s} {'Status':12s}")
    print("-" * 110)
    windows_by_id = {w.window_id: w for w in schedule.windows}
    waves_by_id = {w.wave_id: w for w in schedule.waves}
    for entry in schedule.entries:
        window = windows_by_id.get(entry.window_id)
        wave = waves_by_id.get(entry.wave_id) if entry.wave_id else None
        print(
            f"{entry.component_name:25s} {entry.component_version:10s} "
            f"{(window.name if window else entry.window_id):20s} "
            f"{(wave.name if wave else '-'):15s} "
            f"{(entry.scheduled_start or '-'):25s} "
            f"{entry.status.value:12s}"
        )

    if schedule.unscheduled_components:
        print(f"\nUnscheduled Components:")
        for u in schedule.unscheduled_components:
            reasons = "; ".join(u.get("reasons", []))
            print(f"  - {u['component']} v{u['version']}: {reasons}")

    issues = schedule.issues
    errors = [i for i in issues if i.severity.value == "error"]
    warnings = [i for i in issues if i.severity.value == "warning"]
    if errors:
        print(f"\nErrors:")
        for i in errors:
            comp = f" [{i.component}]" if i.component else ""
            print(f"  - {i.issue_code}{comp}: {i.message}")
    if warnings:
        print(f"\nWarnings:")
        for i in warnings:
            comp = f" [{i.component}]" if i.component else ""
            print(f"  - {i.issue_code}{comp}: {i.message}")

    print(f"\n=== Schedule Summary ===")
    window_entries: dict = {}
    for entry in schedule.entries:
        if entry.window_id not in window_entries:
            window_entries[entry.window_id] = []
        window_entries[entry.window_id].append(entry)
    for wid, entries in window_entries.items():
        window = windows_by_id.get(wid)
        wname = window.name if window else wid
        capacity = window.capacity_max if window else None
        usage = len(entries)
        cap_str = f"{usage}/{capacity}" if capacity else str(usage)
        comps = ", ".join(f"{e.component_name} v{e.component_version}" for e in entries)
        print(f"  {wname}: {cap_str} - {comps}")


def _print_windows(windows) -> None:
    print(f"\nWindows:")
    for w in windows:
        status = "LOCKED" if w.locked else "OPEN"
        cap = f" (capacity: {w.capacity_max})" if w.capacity_max else ""
        lock_info = f" (locked by {w.locked_by} at {w.locked_at})" if w.locked else ""
        envs = f" [{', '.join(w.allowed_environments)}]" if w.allowed_environments else ""
        tz = f" [{w.timezone}]" if w.timezone != "UTC" else ""
        print(
            f"  - [{status}] {w.name} ({w.window_id}){tz}: "
            f"{w.start_time} -> {w.end_time}{cap}{envs}{lock_info}"
        )
        if w.freeze_periods:
            for fp in w.freeze_periods:
                print(f"      FREEZE: {fp.name}: {fp.start} -> {fp.end}" + (f" ({fp.reason})" if fp.reason else ""))


def _format_schedule_summary(schedule: ScheduleResult) -> str:
    """Generate a human-readable schedule summary for export."""
    lines = []
    lines.append(f"# Release Schedule Summary")
    lines.append(f"Schedule ID: {schedule.schedule_id}")
    lines.append(f"Generated at: {schedule.generated_at}")
    lines.append("")
    lines.append(f"## Overview")
    lines.append(f"- Total windows: {len(schedule.windows)}")
    lines.append(f"- Total waves: {len(schedule.waves)}")
    lines.append(f"- Components scheduled: {schedule.total_scheduled}")
    lines.append(f"- Components unscheduled: {schedule.total_unscheduled}")
    lines.append("")

    windows_by_id = {w.window_id: w for w in schedule.windows}
    waves_by_id = {w.wave_id: w for w in schedule.waves}

    lines.append(f"## Windows")
    for w in schedule.windows:
        status = "LOCKED" if w.locked else "OPEN"
        lines.append(f"### [{status}] {w.name} ({w.window_id})")
        lines.append(f"- Time: {w.start_time} -> {w.end_time} ({w.timezone})")
        if w.capacity_max:
            lines.append(f"- Capacity: {w.capacity_max}")
        if w.allowed_environments:
            lines.append(f"- Allowed environments: {', '.join(w.allowed_environments)}")
        if w.required_approval_roles:
            lines.append(f"- Required approvals: {', '.join(w.required_approval_roles)}")
        if w.locked:
            lines.append(f"- Locked by: {w.locked_by} at {w.locked_at}")
        if w.freeze_periods:
            lines.append(f"- Freeze periods:")
            for fp in w.freeze_periods:
                reason = f" - {fp.reason}" if fp.reason else ""
                lines.append(f"  * {fp.name}: {fp.start} -> {fp.end}{reason}")

        w_entries = [e for e in schedule.entries if e.window_id == w.window_id]
        if w_entries:
            lines.append(f"- Components ({len(w_entries)}):")
            for e in w_entries:
                wave = waves_by_id.get(e.wave_id)
                wave_info = f" (wave: {wave.name})" if wave else ""
                lines.append(f"  * {e.component_name} v{e.component_version}{wave_info}")
        lines.append("")

    if schedule.waves:
        lines.append(f"## Waves")
        for wave in schedule.waves:
            lines.append(f"### [{wave.order}] {wave.name} ({wave.wave_id})")
            if wave.description:
                lines.append(f"- {wave.description}")
            w_entries = [e for e in schedule.entries if e.wave_id == wave.wave_id]
            if w_entries:
                lines.append(f"- Components ({len(w_entries)}):")
                for e in w_entries:
                    window = windows_by_id.get(e.window_id)
                    winfo = f" (window: {window.name})" if window else ""
                    lines.append(f"  * {e.component_name} v{e.component_version}{winfo}")
            lines.append("")

    if schedule.unscheduled_components:
        lines.append(f"## Unscheduled Components")
        for u in schedule.unscheduled_components:
            reasons = "; ".join(u.get("reasons", []))
            lines.append(f"- {u['component']} v{u['version']}: {reasons}")
        lines.append("")

    issues = schedule.issues
    errors = [i for i in issues if i.severity.value == "error"]
    warnings = [i for i in issues if i.severity.value == "warning"]
    if errors:
        lines.append(f"## Errors")
        for i in errors:
            comp = f" [{i.component}]" if i.component else ""
            lines.append(f"- {i.issue_code}{comp}: {i.message}")
        lines.append("")
    if warnings:
        lines.append(f"## Warnings")
        for i in warnings:
            comp = f" [{i.component}]" if i.component else ""
            lines.append(f"- {i.issue_code}{comp}: {i.message}")
        lines.append("")

    return "\n".join(lines)
