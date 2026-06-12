"""`rollback-plan` command - generate a rollback plan."""
from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any, List, Optional

from .base import CommandResult
from .plan_cmd import _print_plan
from ..core.models import ReleasePlan
from ..core.rollback import RollbackPlanner
from ..core.validator import ValidationEngine
from ..utils.exit_codes import (
    EXIT_CONFIG_ERROR,
    EXIT_FILE_NOT_FOUND,
    EXIT_LOCK_BLOCKED_OPERATION,
    EXIT_OK,
)
from ..utils.logger import get_logger
from ..utils.storage import check_locks_for_operation, load_json, load_manifest, save_json

LOG = get_logger()
MODULE = "cmd.rollback"


def add_parser(subparsers: "argparse._SubParsersAction") -> None:
    p = subparsers.add_parser("rollback-plan", help="Generate a rollback plan")
    p.add_argument("-m", "--manifest", default="examples/sample_manifest.json",
                   help="Path to manifest JSON")
    p.add_argument("--plan", default=None,
                   help="Optional: path to a release plan JSON (to reverse its order)")
    p.add_argument("-o", "--output", default=None,
                   help="Write rollback plan JSON to this file")
    p.add_argument("--only-failed", action="store_true",
                   help="Only include components listed in --failed")
    p.add_argument("--failed", default=None,
                   help="Comma-separated list of failed component names")
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

    release_plan: Optional[ReleasePlan] = None
    if args.plan:
        try:
            release_plan = ReleasePlan.from_dict(load_json(Path(args.plan)))
        except Exception as exc:
            LOG.warning(MODULE, f"Could not load plan file, ignoring: {exc}")

    failed: List[str] = []
    if args.failed:
        failed = [x.strip() for x in args.failed.split(",") if x.strip()]

    engine = ValidationEngine(manifest)
    validation = engine.validate()
    validation_dict = validation.to_dict()

    env_val = (
        manifest.target_environment.value
        if hasattr(manifest.target_environment, "value")
        else str(manifest.target_environment)
    )
    if args.only_failed and args.failed:
        service_names = [x.strip() for x in args.failed.split(",") if x.strip()]
    elif hasattr(manifest, "components"):
        service_names = [c.name for c in manifest.components]
    else:
        service_names = None
    base_dir = getattr(args, "work_dir", None)
    blockers = check_locks_for_operation(
        base=base_dir,
        environment=env_val,
        service_names=service_names,
    )
    if blockers:
        print(f"\nERROR: Rollback blocked by {len(blockers)} active release lock(s):\n")
        for b in blockers:
            print(f"  [{b.scope.value.upper()}] {b.lock_id} - {b.description_short()}")
            if b.reason:
                print(f"      Reason: {b.reason}")
            print(f"      Created by: {b.created_by}")
        LOG.error(MODULE, f"Rollback blocked by {len(blockers)} active lock(s)")
        return CommandResult(
            exit_code=EXIT_LOCK_BLOCKED_OPERATION.code,
            run_id="",
            manifest_snapshot=manifest.to_dict(),
            validation_result=validation_dict,
            extra_artifacts={
                "blocking_locks": [b.to_dict() for b in blockers],
            },
        )

    rp = RollbackPlanner(manifest, release_plan)
    rollback_plan = rp.generate(only_failed=args.only_failed, failed_components=failed)
    rollback_dict = rollback_plan.to_dict()

    rollback_plan.target_environment = manifest.target_environment
    _print_plan(rollback_plan)
    print(f"\n[Rollback Plan] Action on each step = rollback; order = reverse of release")

    if args.output:
        save_json(rollback_dict, Path(args.output))
        print(f"\nRollback plan written to: {args.output}")

    return CommandResult(
        exit_code=EXIT_OK.code if not rollback_plan.blocked_components else 13,
        run_id="",
        manifest_snapshot=manifest.to_dict(),
        validation_result=validation_dict,
        release_plan=release_plan.to_dict() if release_plan else None,
        rollback_plan=rollback_dict,
    )
