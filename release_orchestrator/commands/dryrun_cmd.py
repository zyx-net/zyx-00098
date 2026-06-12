"""`dry-run` command - simulate a release plan execution."""
from __future__ import annotations

import argparse
from typing import Any

from .base import CommandResult
from ..core.dryrun import DryRunExecutor
from ..core.planner import ReleasePlanner
from ..core.validator import ValidationEngine
from ..utils.exit_codes import (
    EXIT_CONFIG_ERROR,
    EXIT_FILE_NOT_FOUND,
    EXIT_OK,
)
from ..utils.logger import get_logger
from ..utils.policy_loader import PolicyValidationError, load_policy
from ..utils.storage import load_manifest

LOG = get_logger()
MODULE = "cmd.dryrun"


def add_parser(subparsers: "argparse._SubParsersAction") -> None:
    p = subparsers.add_parser("dry-run", aliases=["dryrun"],
                              help="Simulate release plan execution")
    p.add_argument("-m", "--manifest", default="examples/sample_manifest.json",
                   help="Path to manifest JSON")
    p.add_argument("--policy", default=None,
                   help="Path to release policy JSON file")
    p.add_argument("--seed", type=int, default=None,
                   help="Random seed for deterministic simulation")
    p.add_argument("--fail-step", default=None,
                   help="Force a specific component to fail (component name)")
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

    engine = ValidationEngine(manifest, policy=policy)
    validation = engine.validate()
    validation_dict = validation.to_dict()
    exit_code = engine.determine_exit_code()

    planner = ReleasePlanner(manifest, validation)
    plan = planner.generate()
    plan_dict = plan.to_dict()

    executor = DryRunExecutor(manifest, plan, seed=args.seed)
    dry_result = executor.execute(fail_step=args.fail_step)
    summary = dry_result["summary"]
    exit_code = summary["exit_code"] if summary["exit_code"] != 0 else exit_code

    _print_result(dry_result, plan)

    return CommandResult(
        exit_code=exit_code,
        run_id="",
        manifest_snapshot=manifest.to_dict(),
        validation_result=validation_dict,
        release_plan=plan_dict,
        dry_run_result=dry_result,
        extra_artifacts={"policy_snapshot": policy_dict},
    )


def _print_result(dry_result, plan) -> None:
    summary = dry_result["summary"]
    print(f"\n=== Dry-Run Simulation: {summary['dry_run_id']} ===")
    print(f"Release Plan ID : {plan.plan_id}")
    print(f"Target Env      : {plan.target_environment.value}")
    print(f"Total Steps     : {summary['total_steps']}")
    print(f"  Successful    : {summary['successful']}")
    print(f"  Failed        : {summary['failed']}")
    print(f"  Blocked       : {summary['blocked']}")
    print(f"  Skipped       : {summary['skipped']}")
    print(f"Exit Code       : {summary['exit_code']}")
    print(f"Deployed End State: {summary['deployed'] or '(none)'}")
    print()
    print(f"{'#':>3}  {'Component':25s} {'Status':12s} {'Logs (first 2)'}")
    print("-" * 100)
    for step in dry_result["steps"]:
        first_logs = "; ".join(step["logs"][:2]) if step["logs"] else ""
        print(f"{step['step_index']:>3}  {step['component_name']:25s} {step['status']:12s} {first_logs}")
