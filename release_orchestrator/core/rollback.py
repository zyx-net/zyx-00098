"""Rollback plan generator.

Produces a rollback plan that reverses the release plan's execution
order and specifies the rollback target for each component.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from .models import (
    ComponentStatus,
    PlanStep,
    ReleaseManifest,
    ReleasePlan,
    generate_id,
    now_iso,
)
from ..utils.logger import get_logger
from ..utils.exit_codes import EXIT_PLAN_ERROR

LOG = get_logger()
MODULE = "rollback"


class RollbackPlanner:
    """Generates a rollback plan for a given release manifest/plan."""

    def __init__(self, manifest: ReleaseManifest, release_plan: Optional[ReleasePlan] = None):
        self.manifest = manifest
        self.release_plan = release_plan
        self._by_name = {c.name: c for c in manifest.components}

    def generate(self, only_failed: bool = False, failed_components: Optional[List[str]] = None) -> ReleasePlan:
        LOG.info(MODULE, "Generating rollback plan", release_id=self.manifest.release_id)
        failed_components = failed_components or []

        if self.release_plan and self.release_plan.execution_order:
            forward = list(self.release_plan.execution_order)
        else:
            forward = [c.name for c in self.manifest.components]
        reverse_order = list(reversed(forward))

        steps: List[PlanStep] = []
        blocked: List[Dict[str, Any]] = []
        for idx, name in enumerate(reverse_order):
            comp = self._by_name.get(name)
            if not comp:
                continue
            if only_failed and name not in failed_components:
                continue

            rollback_to = comp.rollback_target_version or comp.deployed_version
            blockers: List[str] = []
            if not rollback_to:
                blockers.append("No rollback target (no deployed_version or rollback_target_version)")

            dependents = self._find_dependents(name)
            prereqs = list(dependents)

            step = PlanStep(
                step_index=idx,
                component_name=name,
                component_version=rollback_to or "(unknown)",
                action="rollback",
                prerequisites=prereqs,
                blockers=blockers,
                approver=None,
                rollback_target=comp.version,
                status=ComponentStatus.BLOCKED if blockers else ComponentStatus.SCHEDULED,
                estimated_duration_minutes=3,
                notes=f"Rolling back from {comp.version} to {rollback_to or 'unknown'}",
            )
            steps.append(step)
            if blockers:
                blocked.append({"component": name, "rollback_to": rollback_to, "blockers": blockers})

        total = sum(s.estimated_duration_minutes for s in steps)
        plan = ReleasePlan(
            plan_id=generate_id("RBP"),
            release_id=self.manifest.release_id,
            generated_at=now_iso(),
            target_environment=self.manifest.target_environment,
            execution_order=[s.component_name for s in steps],
            steps=steps,
            blocked_components=blocked,
            total_estimated_minutes=total,
        )
        LOG.info(
            MODULE,
            "Rollback plan generated",
            plan_id=plan.plan_id,
            steps=len(steps),
            blocked=len(blocked),
            total_minutes=total,
        )
        return plan

    def _find_dependents(self, component_name: str) -> List[str]:
        result = []
        for c in self.manifest.components:
            for dep in c.dependencies:
                if dep.name == component_name and dep.required:
                    result.append(c.name)
                    break
        return result
