"""`plan` command - generate a release execution plan."""
from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

from .base import CommandResult
from ..core.planner import ReleasePlanner
from ..core.validator import ValidationEngine
from ..utils.exit_codes import (
    EXIT_CONFIG_ERROR,
    EXIT_FILE_NOT_FOUND,
    EXIT_PLAN_ERROR,
    EXIT_OK,
)
from ..utils.logger import get_logger
from ..utils.storage import load_manifest, save_json

LOG = get_logger()
MODULE = "cmd.plan"


def add_parser(subparsers: "argparse._SubParsersAction") -> None:
    p = subparsers.add_parser("plan", help="Generate a release execution plan")
    p.add_argument("-m", "--manifest", default="examples/sample_manifest.json",
                   help="Path to manifest JSON")
    p.add_argument("-o", "--output", default=None,
                   help="Write plan JSON to this file in addition to stdout")
    p.add_argument("--skip-validate", action="store_true",
                   help="Skip manifest validation before planning")
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

    validation_dict = None
    exit_code = EXIT_OK.code
    if not args.skip_validate:
        engine = ValidationEngine(manifest)
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
        )

    plan_dict = plan.to_dict()
    _print_plan(plan)

    if args.output:
        save_json(plan_dict, Path(args.output))
        print(f"\nPlan written to: {args.output}")

    return CommandResult(
        exit_code=exit_code,
        run_id="",
        manifest_snapshot=manifest.to_dict(),
        validation_result=validation_dict,
        release_plan=plan_dict,
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
