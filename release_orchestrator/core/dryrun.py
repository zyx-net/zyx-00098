"""Dry-run (simulated) execution engine.

Simulates the execution of a release plan step by step, updating
step statuses based on the plan's blocker information and random
(but deterministic) success/failure flags.  Does not perform any
real deployment, git, or CI operations.
"""
from __future__ import annotations

import hashlib
import random
from typing import Any, Dict, List, Optional

from .models import (
    ComponentStatus,
    PlanStep,
    ReleaseManifest,
    ReleasePlan,
    now_iso,
)
from ..utils.logger import get_logger
from ..utils.exit_codes import EXIT_DRYRUN_FAILED

LOG = get_logger()
MODULE = "dryrun"


class DryRunExecutor:
    """Simulates executing a release plan step-by-step."""

    def __init__(self, manifest: ReleaseManifest, plan: ReleasePlan, seed: Optional[int] = None):
        self.manifest = manifest
        self.plan = plan
        if seed is None:
            seed = int(hashlib.md5(plan.plan_id.encode()).hexdigest()[:8], 16)
        self._rng = random.Random(seed)
        self._steps: List[Dict[str, Any]] = []
        self._summary: Dict[str, Any] = {}

    def execute(self, fail_step: Optional[str] = None) -> Dict[str, Any]:
        LOG.info(MODULE, "Starting dry-run simulation", plan_id=self.plan.plan_id)
        deployed: Dict[str, str] = {}
        logs: List[Dict[str, Any]] = []
        any_failed = False

        for step in self.plan.steps:
            step_result = self._simulate_step(step, deployed, logs, fail_step)
            if step_result["status"] == ComponentStatus.FAILED.value:
                any_failed = True
            if step_result["status"] == ComponentStatus.SUCCESS.value:
                deployed[step.component_name] = step.component_version
            self._steps.append(step_result)

        exit_code = EXIT_DRYRUN_FAILED.code if any_failed else 0
        self._summary = {
            "dry_run_id": f"DRY-{self.plan.plan_id}",
            "started_at": self.plan.generated_at,
            "finished_at": now_iso(),
            "total_steps": len(self.plan.steps),
            "successful": len([s for s in self._steps if s["status"] == ComponentStatus.SUCCESS.value]),
            "failed": len([s for s in self._steps if s["status"] == ComponentStatus.FAILED.value]),
            "blocked": len([s for s in self._steps if s["status"] == ComponentStatus.BLOCKED.value]),
            "skipped": len([s for s in self._steps if s["status"] == ComponentStatus.SKIPPED.value]),
            "exit_code": exit_code,
            "deployed": deployed,
            "notes": "This is a simulated execution - no real systems were contacted.",
        }
        LOG.info(
            MODULE,
            "Dry-run complete",
            successful=self._summary["successful"],
            failed=self._summary["failed"],
            blocked=self._summary["blocked"],
            exit_code=exit_code,
        )
        return {
            "summary": self._summary,
            "steps": self._steps,
            "logs": logs,
        }

    def _simulate_step(
        self,
        step: PlanStep,
        deployed: Dict[str, str],
        logs: List[Dict[str, Any]],
        fail_step: Optional[str],
    ) -> Dict[str, Any]:
        step_logs: List[str] = []
        ts = now_iso()

        if step.status == ComponentStatus.BLOCKED or step.blockers:
            status = ComponentStatus.BLOCKED.value
            step_logs.append(f"BLOCKED: {', '.join(step.blockers)}")
            LOG.warning(MODULE, f"Step blocked", step=step.step_index, component=step.component_name, blockers=step.blockers)
        else:
            missing = [p for p in step.prerequisites if p not in deployed]
            if missing:
                status = ComponentStatus.SKIPPED.value
                step_logs.append(f"SKIPPED: prerequisites not met: {', '.join(missing)}")
                LOG.warning(MODULE, f"Step skipped - prerequisites missing", step=step.step_index, missing=missing)
            else:
                will_fail = (fail_step is not None and step.component_name == fail_step)
                if will_fail:
                    status = ComponentStatus.FAILED.value
                    step_logs.append(f"FAILED: injected failure for {step.component_name}")
                    step_logs.append(f"Rollback target: {step.rollback_target or 'unknown'}")
                    LOG.error(MODULE, f"Step failed (injected)", step=step.step_index, component=step.component_name)
                else:
                    r = self._rng.random()
                    if r < 0.02:
                        status = ComponentStatus.FAILED.value
                        step_logs.append(f"FAILED: simulated deployment error (roll={r:.3f})")
                        LOG.error(MODULE, f"Step simulated failure", step=step.step_index, component=step.component_name)
                    else:
                        status = ComponentStatus.SUCCESS.value
                        step_logs.append(f"Applying: {step.component_name}@{step.component_version}")
                        if step.rollback_target:
                            step_logs.append(f"Stored rollback target: {step.rollback_target}")
                        if step.approver:
                            step_logs.append(f"Release approved by: {step.approver}")
                        step_logs.append(f"SUCCESS: {step.component_name} deployed in simulated environment")
                        LOG.info(MODULE, f"Step success", step=step.step_index, component=step.component_name, version=step.component_version)

        return {
            "step_index": step.step_index,
            "component_name": step.component_name,
            "component_version": step.component_version,
            "action": step.action,
            "status": status,
            "started_at": ts,
            "finished_at": now_iso(),
            "rollback_target": step.rollback_target,
            "approver": step.approver,
            "prerequisites": step.prerequisites,
            "blockers": step.blockers,
            "logs": step_logs,
        }
