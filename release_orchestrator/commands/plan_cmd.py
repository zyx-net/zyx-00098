"""`plan` command - generate a release execution plan."""
from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

from .base import CommandResult
from ..core.planner import ReleasePlanner
from ..core.scheduler import SchedulingEngine, load_waves_from_json, load_windows_from_json, load_window_state
from ..core.validator import ValidationEngine
from ..utils.exit_codes import (
    EXIT_CONFIG_ERROR,
    EXIT_FILE_NOT_FOUND,
    EXIT_PLAN_ERROR,
    EXIT_OK,
)
from ..utils.logger import get_logger
from ..utils.policy_loader import PolicyValidationError, load_policy
from ..utils.storage import load_manifest, save_json

LOG = get_logger()
MODULE = "cmd.plan"


def add_parser(subparsers: "argparse._SubParsersAction") -> None:
    p = subparsers.add_parser("plan", help="Generate a release execution plan")
    p.add_argument("-m", "--manifest", default="examples/sample_manifest.json",
                   help="Path to manifest JSON")
    p.add_argument("--policy", default=None,
                   help="Path to release policy JSON file")
    p.add_argument("-o", "--output", default=None,
                   help="Write plan JSON to this file in addition to stdout")
    p.add_argument("--skip-validate", action="store_true",
                   help="Skip manifest validation before planning")
    p.add_argument("-w", "--windows", default=None,
                   help="Path to windows configuration (JSON or CSV) for scheduling")
    p.add_argument("--waves", default=None,
                   help="Path to waves configuration JSON for scheduling")
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

    policy_dict = policy.to_dict()

    validation_dict = None
    exit_code = EXIT_OK.code
    if not args.skip_validate:
        engine = ValidationEngine(manifest, policy=policy)
        validation = engine.validate()
        validation_dict = validation.to_dict()
        exit_code = engine.determine_exit_code()
    else:
        validation = None

    planner = ReleasePlanner(manifest, validation)
    try:
        plan = planner.generate()
    except Exception as exc:
        LOG.error(MODULE, f"Plan generation failed: {exc}")
        return CommandResult(
            exit_code=EXIT_PLAN_ERROR.code,
            run_id="",
            manifest_snapshot=manifest.to_dict(),
            validation_result=validation_dict,
            extra_artifacts={"policy_snapshot": policy_dict},
        )

    plan_dict = plan.to_dict()
    _print_plan(plan)

    schedule_result = None
    schedule_dict = None
    if args.windows or (hasattr(manifest, "release_windows") and manifest.release_windows):
        windows = []
        if args.windows:
            if args.windows.lower().endswith(".csv"):
                from ..core.scheduler import load_windows_from_csv
                windows = load_windows_from_csv(args.windows)
            else:
                windows = load_windows_from_json(args.windows)
        elif hasattr(manifest, "release_windows"):
            windows = manifest.release_windows

        if windows:
            waves = None
            if args.waves:
                waves = load_waves_from_json(args.waves)
            elif hasattr(manifest, "waves") and manifest.waves:
                waves = manifest.waves

            load_window_state(windows, base=getattr(args, "work_dir", None))

            scheduler = SchedulingEngine(manifest, windows, waves, policy, validation)
            schedule_result = scheduler.schedule()
            schedule_dict = schedule_result.to_dict()
            exit_code = scheduler.determine_exit_code() or exit_code
            _print_schedule_summary(schedule_result)

            _merge_schedule_into_plan(plan_dict, schedule_result)

    if args.output:
        save_json(plan_dict, Path(args.output))
        print(f"\nPlan written to: {args.output}")

    extra = {"policy_snapshot": policy_dict}
    if schedule_dict:
        extra["schedule_result"] = schedule_dict

    return CommandResult(
        exit_code=exit_code,
        run_id="",
        manifest_snapshot=manifest.to_dict(),
        validation_result=validation_dict,
        release_plan=plan_dict,
        schedule_result=schedule_dict,
        extra_artifacts=extra,
    )


def _print_plan(plan) -> None:
    print(f"\n=== Release Plan: {plan.plan_id} ===")
    print(f"Release ID        : {plan.release_id}")
    print(f"Target Env        : {plan.target_environment.value}")
    print(f"Generated At      : {plan.generated_at}")
    print(f"Total Est. Time   : {plan.total_estimated_minutes} min")
    print(f"Steps             : {len(plan.steps)}")
    print(f"Blocked           : {len(plan.blocked_components)}")
    print(f"\nExecution Order   : {' -> '.join(plan.execution_order) or '(empty)'}")
    print()
    print(f"{'#':>3}  {'Component':25s} {'Ver':10s} {'Action':9s} {'Status':10s} {'Approver':18s} {'Rollback':12s} Blockers")
    print("-" * 120)
    for step in plan.steps:
        blockers = ", ".join(step.blockers) if step.blockers else "-"
        approver = step.approver or "(unassigned)"
        rollback = step.rollback_target or "-"
        print(f"{step.step_index:>3}  {step.component_name:25s} {step.component_version:10s} "
              f"{step.action:9s} {step.status.value:10s} {approver:18s} {rollback:12s} {blockers}")
    if plan.blocked_components:
        print(f"\nBlocked Components Summary:")
        for b in plan.blocked_components:
            print(f"  - {b['component']} v{b['version']}: {', '.join(b['blockers'])}")


def _print_schedule_summary(schedule_result) -> None:
    """Print a short schedule summary alongside the plan."""
    print(f"\n--- Schedule Summary ---")
    print(f"Schedule ID : {schedule_result.schedule_id}")
    print(f"Windows     : {len(schedule_result.windows)}")
    print(f"Waves       : {len(schedule_result.waves)}")
    print(f"Scheduled   : {schedule_result.total_scheduled}")
    print(f"Unscheduled : {schedule_result.total_unscheduled}")
    if schedule_result.entries:
        print(f"\nScheduled Components:")
        for e in schedule_result.entries:
            wave = f" wave={e.wave_id}" if e.wave_id else ""
            print(f"  {e.component_name} v{e.component_version} -> {e.window_id}{wave}")
    if schedule_result.unscheduled_components:
        print(f"\nUnscheduled Components:")
        for u in schedule_result.unscheduled_components:
            reasons = "; ".join(u.get("reasons", []))
            print(f"  {u['component']} v{u['version']}: {reasons}")


def _merge_schedule_into_plan(plan_dict: dict, schedule_result) -> None:
    """Merge window/wave/schedule info into the plan dict for downstream consumers."""
    step_map: dict = {}
    for step in plan_dict.get("steps", []):
        step_map[step["component_name"]] = step

    for entry in schedule_result.entries:
        step = step_map.get(entry.component_name)
        if step:
            step["window_id"] = entry.window_id
            step["wave_id"] = entry.wave_id
            step["scheduled_start"] = entry.scheduled_start

    plan_dict["schedule_id"] = schedule_result.schedule_id
    plan_dict["schedule_generated_at"] = schedule_result.generated_at
    plan_dict["total_scheduled"] = schedule_result.total_scheduled
    plan_dict["total_unscheduled"] = schedule_result.total_unscheduled
    plan_dict["windows"] = [w.to_dict() if hasattr(w, "to_dict") else w for w in schedule_result.windows]
    plan_dict["waves"] = [w.to_dict() if hasattr(w, "to_dict") else w for w in schedule_result.waves]
