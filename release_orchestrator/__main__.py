"""Main CLI entry point for the release orchestrator tool.

Usage:
    python -m release_orchestrator <command> [options]
    python release_orchestrator_cli.py <command> [options]

Commands:
    init            Generate sample manifest + artifact files
    validate        Validate a manifest (versions, deps, checksums, approvals)
    plan            Generate a release execution plan
    dry-run         Simulate executing the release plan
    rollback-plan   Generate a rollback plan
    export          Export manifest + plans + results into an archive
    history         List / inspect past command runs
    exit-codes      Print documentation for all exit codes

Every command stores a full snapshot (config, manifest, validation,
plans, logs) under .release_orchestrator/history/<run_id>/ for later
inspection and to guarantee consistency between logs, plans, and exports.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import List, Optional

from .commands.base import run_with_snapshot
from .commands.init_cmd import add_parser as add_init
from .commands.validate_cmd import add_parser as add_validate
from .commands.plan_cmd import add_parser as add_plan
from .commands.dryrun_cmd import add_parser as add_dryrun
from .commands.rollback_cmd import add_parser as add_rollback
from .commands.export_cmd import add_parser as add_export
from .commands.history_cmd import add_parser as add_history
from .commands.schedule_cmd import add_parser as add_schedule
from .utils.exit_codes import (
    ALL_EXIT_CODES,
    EXIT_INTERNAL_ERROR,
    EXIT_UNKNOWN_COMMAND,
    exit_codes_as_dict,
)
from .utils.logger import get_logger
from .utils.storage import save_json


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="release-orchestrator",
        description="Multi-command offline release package orchestrator.",
    )
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="Enable verbose (DEBUG) logging")
    parser.add_argument("--work-dir", default=None,
                        help="Override base work directory (default: CWD/.release_orchestrator)")
    parser.add_argument("--version", action="version", version="%(prog)s 1.0.0")

    sub = parser.add_subparsers(dest="command", metavar="<command>")

    add_init(sub)
    add_validate(sub)
    add_plan(sub)
    add_dryrun(sub)
    add_rollback(sub)
    add_export(sub)
    add_history(sub)
    add_schedule(sub)

    # exit-codes pseudo command (not wrapped in snapshot - it's a doc helper)
    p = sub.add_parser("exit-codes", help="Print exit code documentation")
    p.add_argument("-o", "--output", default=None,
                   help="Write exit code docs as JSON to a file in addition to stdout")
    p.add_argument("--json", action="store_true", dest="as_json",
                   help="Print as JSON instead of human-readable text")

    return parser


def _print_exit_codes(as_json: bool = False, output: Optional[str] = None) -> int:
    codes = exit_codes_as_dict()
    if output:
        save_json(codes, Path(output))
        print(f"Exit codes written to: {output}")
    if as_json:
        print(json.dumps(codes, indent=2, ensure_ascii=False))
        return 0

    print("\n=== Release Orchestrator Exit Codes ===\n")
    print(f"{'Code':>5}  {'Name':35s} Description")
    print("-" * 100)
    for code in sorted(ALL_EXIT_CODES, key=lambda e: e.code):
        print(f"{code.code:>5}  {code.name:35s} {code.description}")
    print()
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    get_logger(verbose=args.verbose, reset=True)

    has_func = hasattr(args, "func") and args.func is not None
    if not args.command and not has_func:
        parser.print_help()
        return EXIT_UNKNOWN_COMMAND.code

    if args.command == "exit-codes" or (not args.command and has_func and getattr(args.func, "__name__", "") == "_print_exit_codes"):
        return _print_exit_codes(getattr(args, "as_json", False), getattr(args, "output", None))

    if not has_func:
        print(f"Unknown command: {args.command}", file=sys.stderr)
        parser.print_help()
        return EXIT_UNKNOWN_COMMAND.code

    if args.command:
        command_name = args.command
    else:
        mod = getattr(args.func, "__module__", "")
        if "init" in mod:
            command_name = "init"
        elif "validate" in mod:
            command_name = "validate"
        elif "plan" in mod:
            command_name = "plan"
        elif "dryrun" in mod:
            command_name = "dry-run"
        elif "rollback" in mod:
            command_name = "rollback-plan"
        elif "export" in mod:
            command_name = "export"
        elif "history" in mod:
            command_name = "history"
        elif "schedule" in mod:
            command_name = "schedule"
        else:
            command_name = "unknown"

    def _runner(args_obj, run_id=None):
        return args_obj.func(args_obj, run_id=run_id)

    try:
        return run_with_snapshot(command_name, _runner, args, args.work_dir)
    except KeyboardInterrupt:
        print("\nInterrupted by user.", file=sys.stderr)
        return 130
    except Exception as exc:
        print(f"Internal error: {exc}", file=sys.stderr)
        import traceback
        traceback.print_exc()
        return EXIT_INTERNAL_ERROR.code


if __name__ == "__main__":
    sys.exit(main())
