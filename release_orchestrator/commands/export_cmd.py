"""`export` command - bundle manifest, plans, and results into an archive."""
from __future__ import annotations

import argparse
from typing import Any

from .base import CommandResult
from ..core.compare import compare_snapshots
from ..core.dryrun import DryRunExecutor
from ..core.planner import ReleasePlanner
from ..core.rollback import RollbackPlanner
from ..core.validator import ValidationEngine
from ..utils.exit_codes import (
    EXIT_CONFIG_ERROR,
    EXIT_EXPORT_ERROR,
    EXIT_FILE_NOT_FOUND,
    EXIT_HISTORY_ERROR,
    EXIT_OK,
)
from ..utils.logger import get_logger
from ..utils.storage import (
    export_full_manifest_archive,
    get_snapshot,
    load_manifest,
)

LOG = get_logger()
MODULE = "cmd.export"


def add_parser(subparsers: "argparse._SubParsersAction") -> None:
    p = subparsers.add_parser("export", help="Export manifest + all artifacts into an archive")
    p.add_argument("-m", "--manifest", default="examples/sample_manifest.json",
                   help="Path to manifest JSON")
    p.add_argument("-o", "--output", default="archives/release_bundle",
                   help="Output archive path (without extension)")
    p.add_argument("--format", choices=["zip", "tar", "tar.gz"], default="zip",
                   help="Archive format (default: zip)")
    p.add_argument("--no-dryrun", action="store_true",
                   help="Skip dry-run execution before export")
    p.add_argument("--seed", type=int, default=None,
                   help="Random seed for dry-run simulation")
    p.add_argument("--compare-with", default=None, metavar="RUN_ID",
                   help="Include a compare_report.json comparing against the given historical run")
    p.set_defaults(func=_run)


def _run(args: argparse.Namespace, run_id: str = "", **_: Any) -> CommandResult:
    try:
        manifest = load_manifest(args.manifest)
    except FileNotFoundError:
        LOG.error(MODULE, f"Manifest not found: {args.manifest}")
        return CommandResult(exit_code=EXIT_FILE_NOT_FOUND.code, run_id="")
    except ValueError as exc:
        LOG.error(MODULE, f"Invalid manifest: {exc}")
        return CommandResult(exit_code=EXIT_CONFIG_ERROR.code, run_id="")

    engine = ValidationEngine(manifest)
    validation = engine.validate()
    validation_dict = validation.to_dict()

    planner = ReleasePlanner(manifest, validation)
    plan = planner.generate()
    plan_dict = plan.to_dict()

    rollback = RollbackPlanner(manifest, plan).generate()
    rollback_dict = rollback.to_dict()

    dry_run_result = None
    if not args.no_dryrun:
        executor = DryRunExecutor(manifest, plan, seed=args.seed)
        dry_run_result = executor.execute()

    # Snapshot the config and log text BEFORE we append any extra export-
    # related log lines, so the values written to the zip match what
    # base.run_with_snapshot will later persist under history/<run_id>/.
    args_dict = vars(args).copy()
    args_dict["func"] = repr(args_dict.get("func", _run))
    config_dict = {
        "args": args_dict,
        "command": "export",
        "run_id": run_id,
    }
    log_text = get_logger().get_text()

    extra = {
        "validation.json": validation_dict,
        "release_plan.json": plan_dict,
        "rollback_plan.json": rollback_dict,
        "config.json": config_dict,
        "run.log": log_text,
    }
    if dry_run_result:
        extra["dry_run_result.json"] = dry_run_result

    compare_report = None
    if args.compare_with:
        from ..core.models import ExecutionSnapshot
        base = getattr(args, "work_dir", None)
        ref_snap = get_snapshot(args.compare_with, base=base)
        if not ref_snap:
            LOG.error(MODULE, f"Compare reference run not found: {args.compare_with}")
            print(f"Error: reference run_id not found: {args.compare_with}")
            return CommandResult(
                exit_code=EXIT_HISTORY_ERROR.code,
                run_id="",
                manifest_snapshot=manifest.to_dict(),
                validation_result=validation_dict,
                release_plan=plan_dict,
                rollback_plan=rollback_dict,
                dry_run_result=dry_run_result,
            )

        current_snap = ExecutionSnapshot(
            run_id=run_id,
            command="export",
            started_at="",
            finished_at="",
            exit_code=0,
            config_snapshot=config_dict,
            manifest_snapshot=manifest.to_dict(),
            validation_result=validation_dict,
            release_plan=plan_dict,
            rollback_plan=rollback_dict,
            dry_run_result=dry_run_result,
            logs=[],
        )

        # 注意：这里以 ref_snap 为 A，current_snap 为 B
        # 这样报告里 A 是历史参考，B 是本次导出
        report = compare_snapshots(ref_snap, current_snap, base=base)
        compare_report = report.to_dict()
        # 记录来源 run_id
        compare_report["source_run_a"] = args.compare_with
        compare_report["source_run_b"] = run_id
        extra["compare_report.json"] = compare_report

    exit_code = EXIT_OK.code
    try:
        archive_path = export_full_manifest_archive(
            manifest=manifest,
            output_path=args.output,
            extra_files=extra,
            fmt=args.format,
        )
    except Exception as exc:
        LOG.error(MODULE, f"Export failed: {exc}")
        return CommandResult(
            exit_code=EXIT_EXPORT_ERROR.code,
            run_id="",
            manifest_snapshot=manifest.to_dict(),
            validation_result=validation_dict,
            release_plan=plan_dict,
            rollback_plan=rollback_dict,
            dry_run_result=dry_run_result,
        )

    print(f"\n=== Export Complete ===")
    print(f"Manifest      : {manifest.release_id}")
    print(f"Components    : {len(manifest.components)}")
    print(f"Archive       : {archive_path}")
    print(f"Format        : {args.format}")
    print(f"Included files:")
    for name in ["manifest.json"] + list(extra.keys()):
        print(f"  - {name}")
    if compare_report:
        print(f"\nCompare report included:")
        print(f"  Reference run: {args.compare_with}")
        print(f"  Current run  : {run_id}")
        warns = compare_report.get("warnings", [])
        if warns:
            print(f"  Warnings     : {len(warns)}")

    return CommandResult(
        exit_code=exit_code,
        run_id="",
        manifest_snapshot=manifest.to_dict(),
        validation_result=validation_dict,
        release_plan=plan_dict,
        rollback_plan=rollback_dict,
        dry_run_result=dry_run_result,
        extra_artifacts={
            "archive_path": str(archive_path),
            "compare_report": compare_report,
        } if compare_report else {"archive_path": str(archive_path)},
    )
