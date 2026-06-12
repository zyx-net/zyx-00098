"""Release plan generator - topological sort + metadata.

Takes a validated manifest and produces:
- Execution order (topological based on dependencies)
- Per-step prerequisites, blockers, approver, rollback target
- A list of blocked components (unsatisfiable deps, missing approvals, etc.)
"""
from __future__ import annotations

from collections import defaultdict, deque
from typing import Any, Dict, List, Optional, Set, Tuple

from .models import (
    ApprovalStatus,
    Component,
    ComponentStatus,
    EnvironmentType,
    PlanStep,
    ReleaseManifest,
    ReleasePlan,
    Severity,
    ValidationResult,
    generate_id,
    now_iso,
)
from .validator import ValidationEngine
from ..utils.logger import get_logger
from ..utils.exit_codes import EXIT_PLAN_ERROR

LOG = get_logger()
MODULE = "planner"


class ReleasePlanner:
    """Generates a release execution plan from a validated manifest."""

    def __init__(self, manifest: ReleaseManifest, validation: Optional[ValidationResult] = None):
        self.manifest = manifest
        self.validation = validation
        self._by_name: Dict[str, Component] = {c.name: c for c in manifest.components}

    def generate(self) -> ReleasePlan:
        LOG.info(MODULE, "Generating release plan", release_id=self.manifest.release_id)
        order = self._topological_order()
        steps: List[PlanStep] = []
        blocked: List[Dict[str, Any]] = []
        name_to_step_index: Dict[str, int] = {}

        error_components = set()
        if self.validation:
            for issue in self.validation.issues:
                if issue.severity == Severity.ERROR and issue.component:
                    error_components.add(issue.component)

        for idx, name in enumerate(order):
            comp = self._by_name[name]
            prereqs = [d.name for d in comp.dependencies if d.required and d.name in self._by_name]
            blockers = self._compute_blockers(comp, prereqs, error_components)
            approver = self._find_approver(comp)
            rollback_target = comp.rollback_target_version or comp.deployed_version
            status = ComponentStatus.BLOCKED if blockers else ComponentStatus.SCHEDULED

            step = PlanStep(
                step_index=idx,
                component_name=comp.name,
                component_version=comp.version,
                action="deploy",
                prerequisites=prereqs,
                blockers=blockers,
                approver=approver,
                rollback_target=rollback_target,
                status=status,
                estimated_duration_minutes=max(5, len(prereqs) * 3),
                notes=comp.description,
            )
            steps.append(step)
            name_to_step_index[name] = idx
            if blockers:
                blocked.append({
                    "component": name,
                    "version": comp.version,
                    "blockers": blockers,
                })

        for comp in self.manifest.components:
            if comp.name not in name_to_step_index:
                blocked.append({
                    "component": comp.name,
                    "version": comp.version,
                    "blockers": ["Not schedulable - excluded from order"],
                })

        total = sum(s.estimated_duration_minutes for s in steps)
        plan = ReleasePlan(
            plan_id=generate_id("PLAN"),
            release_id=self.manifest.release_id,
            generated_at=now_iso(),
            target_environment=self.manifest.target_environment,
            execution_order=order,
            steps=steps,
            blocked_components=blocked,
            total_estimated_minutes=total,
        )
        LOG.info(
            MODULE,
            "Release plan generated",
            plan_id=plan.plan_id,
            steps=len(steps),
            blocked=len(blocked),
            total_minutes=total,
        )
        return plan

    def _topological_order(self) -> List[str]:
        graph: Dict[str, List[str]] = defaultdict(list)
        indegree: Dict[str, int] = {c.name: 0 for c in self.manifest.components}
        for c in self.manifest.components:
            for dep in c.dependencies:
                if dep.required and dep.name in indegree:
                    graph[dep.name].append(c.name)
                    indegree[c.name] += 1
        q = deque([n for n, d in indegree.items() if d == 0])
        result: List[str] = []
        while q:
            node = q.popleft()
            result.append(node)
            for nxt in graph[node]:
                indegree[nxt] -= 1
                if indegree[nxt] == 0:
                    q.append(nxt)
        if len(result) != len(self.manifest.components):
            missing = [n for n in indegree if n not in result]
            LOG.warning(MODULE, "Topological sort incomplete - excluded nodes", excluded=missing)
        return result

    def _compute_blockers(
        self,
        comp: Component,
        prereqs: List[str],
        error_components: Set[str],
    ) -> List[str]:
        blockers: List[str] = []
        for prereq in prereqs:
            if prereq in error_components:
                blockers.append(f"Prerequisite {prereq} has validation errors")
        needs_approval = comp.environment in (EnvironmentType.PRODUCTION, EnvironmentType.STAGING)
        if needs_approval:
            has_approved = any(a.status == ApprovalStatus.APPROVED for a in comp.approvals)
            if not has_approved:
                blockers.append("Missing required production approval")
        return blockers

    def _find_approver(self, comp: Component) -> Optional[str]:
        approved = [a for a in comp.approvals if a.status == ApprovalStatus.APPROVED]
        if approved:
            return approved[-1].approver
        return None


def validate_and_plan(manifest_path: str) -> Tuple[Optional[ReleasePlan], Optional[ValidationResult], int]:
    """Convenience helper: load, validate, and plan."""
    from ..utils.storage import load_manifest
    try:
        manifest = load_manifest(manifest_path)
    except FileNotFoundError:
        LOG.error(MODULE, f"Manifest not found: {manifest_path}")
        return None, None, EXIT_PLAN_ERROR.code
    except ValueError as exc:
        LOG.error(MODULE, f"Invalid manifest: {exc}")
        return None, None, EXIT_PLAN_ERROR.code

    engine = ValidationEngine(manifest)
    validation = engine.validate()
    exit_code = engine.determine_exit_code()
    if exit_code != 0:
        LOG.warning(MODULE, f"Validation failed (exit={exit_code}), still attempting plan generation")

    planner = ReleasePlanner(manifest, validation)
    try:
        plan = planner.generate()
    except Exception as exc:
        LOG.error(MODULE, f"Plan generation failed: {exc}")
        return None, validation, EXIT_PLAN_ERROR.code
    return plan, validation, 0 if exit_code == 0 else exit_code
