"""Command base infrastructure and shared helpers.

Each command exposes a `run(args, logger) -> exit_code` function and
accepts an argparse.Namespace-style `args` object.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Tuple

from ..utils.logger import get_logger, OrchestratorLogger
from ..utils.exit_codes import EXIT_INTERNAL_ERROR, EXIT_OK
from ..utils.storage import ensure_work_dir, persist_run_artifacts
from ..core.models import now_iso, generate_id


LOG: OrchestratorLogger = get_logger()


@dataclass
class CommandResult:
    exit_code: int
    run_id: str
    manifest_snapshot: Optional[Dict[str, Any]] = None
    validation_result: Optional[Dict[str, Any]] = None
    release_plan: Optional[Dict[str, Any]] = None
    rollback_plan: Optional[Dict[str, Any]] = None
    dry_run_result: Optional[Dict[str, Any]] = None
    schedule_result: Optional[Dict[str, Any]] = None
    schedule_summary: Optional[str] = None
    config_snapshot: Optional[Dict[str, Any]] = None
    extra_artifacts: Optional[Dict[str, Any]] = None


def run_with_snapshot(
    command_name: str,
    fn: Callable[..., CommandResult],
    args: argparse.Namespace,
    work_base: Optional[str] = None,
) -> int:
    """Run a command, capture state, persist snapshot, return exit code."""
    ensure_work_dir(work_base or getattr(args, "work_dir", None))
    run_id = generate_id("RUN")
    started_at = now_iso()
    config_snapshot = {"args": vars(args), "command": command_name, "run_id": run_id}

    logger = get_logger()
    logger.clear()
    logger.info("cli", f"[{command_name}] Starting execution", run_id=run_id)
    try:
        result = fn(args, run_id=run_id)
    except Exception as exc:
        logger.error("cli", f"Unhandled exception during '{command_name}': {exc}")
        import traceback
        traceback.print_exc()
        result = CommandResult(
            exit_code=EXIT_INTERNAL_ERROR.code,
            run_id=run_id,
            config_snapshot={"error": str(exc), **config_snapshot},
        )

    result.run_id = run_id
    result.config_snapshot = result.config_snapshot or config_snapshot
    logger.info("cli", f"[{command_name}] Finished", run_id=run_id, exit_code=result.exit_code)

    extra = result.extra_artifacts or {}
    policy_snapshot = extra.get("policy_snapshot")
    policy_summary = extra.get("policy_summary")

    schedule_result = result.schedule_result
    schedule_summary = result.schedule_summary
    if schedule_result is None and "schedule_result" in extra:
        schedule_result = extra["schedule_result"]
    if schedule_summary is None and "schedule_summary" in extra:
        schedule_summary = extra["schedule_summary"]

    persist_run_artifacts(
        run_id=run_id,
        command=command_name,
        started_at=started_at,
        exit_code=result.exit_code,
        config_snapshot=result.config_snapshot,
        manifest_snapshot=result.manifest_snapshot,
        policy_snapshot=policy_snapshot,
        policy_summary=policy_summary,
        validation_result=result.validation_result,
        release_plan=result.release_plan,
        rollback_plan=result.rollback_plan,
        dry_run_result=result.dry_run_result,
        schedule_result=schedule_result,
        schedule_summary=schedule_summary,
        logs_text=logger.get_text(),
        logs_entries=logger.get_entries(),
        base=work_base or getattr(args, "work_dir", None),
    )
    return result.exit_code
