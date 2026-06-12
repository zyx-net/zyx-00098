"""`history` command - query and display execution history."""
from __future__ import annotations

import argparse
import json
from typing import Any

from .base import CommandResult
from ..core.compare import compare_snapshots, format_report_text
from ..utils.exit_codes import EXIT_HISTORY_ERROR, EXIT_OK, get_exit_code_by_code
from ..utils.logger import get_logger
from ..utils.storage import (
    HISTORY_DIR,
    LOG_FILE,
    get_snapshot,
    list_history,
    load_json,
)
from pathlib import Path

LOG = get_logger()
MODULE = "cmd.history"


def add_parser(subparsers: "argparse._SubParsersAction") -> None:
    p = subparsers.add_parser("history", help="Query and display execution history")
    p.add_argument("-l", "--limit", type=int, default=20,
                   help="Maximum number of history entries to show")
    p.add_argument("--show", default=None, metavar="RUN_ID",
                   help="Show full details for a specific run_id")
    p.add_argument("--show-logs", action="store_true",
                   help="When used with --show, print the run's log text")
    p.add_argument("--command", default=None,
                   help="Filter by command name (init/validate/plan/...)")
    p.add_argument("--json", action="store_true", dest="as_json",
                   help="Output as JSON instead of human-readable text")
    p.add_argument("compare", nargs="?", default=None, metavar="compare",
                   help=argparse.SUPPRESS)
    p.add_argument("run_a", nargs="?", default=None, metavar="run_a",
                   help=argparse.SUPPRESS)
    p.add_argument("run_b", nargs="?", default=None, metavar="run_b",
                   help=argparse.SUPPRESS)
    p.set_defaults(func=_run)


def _run(args: argparse.Namespace, **_: Any) -> CommandResult:
    if args.compare == "compare" and args.run_a and args.run_b:
        return _compare_runs(args, args.run_a, args.run_b)
    if args.compare and not args.run_a:
        args.compare = None
    if args.show:
        return _show_run(args)
    return _list_runs(args)


def _list_runs(args: argparse.Namespace) -> CommandResult:
    history = list_history()
    if args.command:
        history = [h for h in history if h.get("command") == args.command]
    history = history[: args.limit]

    if not history:
        print("No execution history found. Run some commands first.")
        return CommandResult(exit_code=EXIT_OK.code, run_id="")

    if args.as_json:
        print(json.dumps(history, indent=2, ensure_ascii=False, default=str))
        return CommandResult(exit_code=EXIT_OK.code, run_id="")

    print(f"\n=== Execution History (last {len(history)}) ===")
    print(f"{'Run ID':40s} {'Command':14s} {'Exit':>5s} {'Started At':20s} {'Finished At':20s}")
    print("-" * 110)
    for h in history:
        ec = h.get("exit_code", -1)
        ec_obj = get_exit_code_by_code(int(ec))
        ec_str = f"{ec} ({ec_obj.name})"
        print(
            f"{h.get('run_id', '?'):40s} "
            f"{h.get('command', '?'):14s} "
            f"{ec_str:>20s} "
            f"{h.get('started_at', '?'):20s} "
            f"{h.get('finished_at', '?'):20s}"
        )
    print(f"\nTip: use --show <RUN_ID> to inspect a specific run.")
    print(f"Tip: use history compare <run_a> <run_b> to compare two runs.")
    return CommandResult(exit_code=EXIT_OK.code, run_id="")


def _show_run(args: argparse.Namespace) -> CommandResult:
    run_id = args.show
    snap = get_snapshot(run_id)
    if not snap:
        print(f"Run ID not found: {run_id}")
        return CommandResult(exit_code=EXIT_HISTORY_ERROR.code, run_id="")

    sd = snap.to_dict()
    ec = get_exit_code_by_code(int(snap.exit_code))

    if args.as_json:
        print(json.dumps(sd, indent=2, ensure_ascii=False, default=str))
        return CommandResult(exit_code=EXIT_OK.code, run_id="")

    print(f"\n=== Execution Details: {snap.run_id} ===")
    print(f"Command     : {snap.command}")
    print(f"Started     : {snap.started_at}")
    print(f"Finished    : {snap.finished_at or '(incomplete)'}")
    print(f"Exit Code   : {snap.exit_code} - {ec.name}")
    print(f"            : {ec.description}")

    def _section(title: str, present: bool) -> None:
        mark = "YES" if present else "NO"
        print(f"  {mark:>4s}  {title}")

    print(f"\nArtifacts present:")
    _section("Config snapshot", snap.config_snapshot is not None)
    _section("Manifest snapshot", snap.manifest_snapshot is not None)
    _section("Policy snapshot", snap.policy_snapshot is not None)
    _section("Policy summary", snap.policy_summary is not None)
    _section("Validation result", snap.validation_result is not None)
    _section("Release plan", snap.release_plan is not None)
    _section("Rollback plan", snap.rollback_plan is not None)
    _section("Dry-run result", snap.dry_run_result is not None)
    _section("Schedule result", snap.schedule_result is not None)
    _section("Logs", bool(snap.logs))
    if snap.archive_path:
        print(f"   YES  Export archive: {snap.archive_path}")

    if snap.validation_result:
        vr = snap.validation_result
        summary = vr.get("summary", {})
        print(f"\nValidation summary: {summary}")

    if snap.release_plan:
        rp = snap.release_plan
        order = rp.get("execution_order", [])
        print(f"Plan execution order: {' -> '.join(order) or '(none)'}")
        blocked = rp.get("blocked_components", [])
        if blocked:
            print(f"Blocked components: {len(blocked)}")

    if snap.schedule_result:
        sr = snap.schedule_result
        print(f"\nSchedule summary:")
        print(f"  Schedule ID   : {sr.get('schedule_id', 'unknown')}")
        print(f"  Windows       : {len(sr.get('windows', []))}")
        print(f"  Waves         : {len(sr.get('waves', []))}")
        print(f"  Scheduled     : {sr.get('total_scheduled', 0)}")
        print(f"  Unscheduled   : {sr.get('total_unscheduled', 0)}")
        entries = sr.get("entries", [])
        if entries:
            print(f"  Components:")
            for e in entries[:10]:
                wave = f" wave {e.get('wave_id', '?')}" if e.get('wave_id') else ""
                print(f"    - {e.get('component_name', '?')} v{e.get('component_version', '?')}"
                      f" -> {e.get('window_id', '?')}{wave}")
            if len(entries) > 10:
                print(f"    ... and {len(entries) - 10} more")

    if snap.policy_summary:
        ps = snap.policy_summary
        print(f"\nPolicy summary:")
        print(f"  Target environment : {ps.get('target_environment', 'unknown')}")
        print(f"  Policy version     : {ps.get('policy_version', 'unknown')}")
        rules = ps.get("rules_applied", [])
        print(f"  Rules applied      : {', '.join(rules) if rules else '(none)'}")
        if ps.get("warnings"):
            print(f"  Policy warnings    : {len(ps['warnings'])}")
            for w in ps["warnings"][:3]:
                print(f"    - {w}")

    if args.show_logs:
        log_path = Path(snap.get("run_dir", "")) / LOG_FILE if False else None
        # Fallback: render from entries
        logs = snap.logs or []
        if logs:
            print(f"\n=== Log Output ({len(logs)} entries) ===")
            for entry in logs:
                extra = f" {entry.get('extra', '')}" if entry.get("extra") else ""
                print(f"[{entry.get('timestamp')}] [{entry.get('level'):7s}] [{entry.get('module')}] {entry.get('message')}{extra}")
        else:
            print("\n(No log entries stored in snapshot)")

    return CommandResult(exit_code=EXIT_OK.code, run_id="")


def _compare_runs(args: argparse.Namespace, run_a: str, run_b: str) -> CommandResult:
    snap_a = get_snapshot(run_a)
    snap_b = get_snapshot(run_b)

    if not snap_a:
        print(f"Run ID not found: {run_a}")
        return CommandResult(exit_code=EXIT_HISTORY_ERROR.code, run_id="")
    if not snap_b:
        print(f"Run ID not found: {run_b}")
        return CommandResult(exit_code=EXIT_HISTORY_ERROR.code, run_id="")

    base = getattr(args, "work_dir", None)
    report = compare_snapshots(snap_a, snap_b, base=base)

    if args.as_json:
        print(json.dumps(report.to_dict(), indent=2, ensure_ascii=False, default=str))
    else:
        print(format_report_text(report))

    return CommandResult(exit_code=EXIT_OK.code, run_id="")
