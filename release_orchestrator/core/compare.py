"""Execution snapshot comparison utilities.

Provides logic to diff two historical run snapshots across all
persisted artifacts (config, manifest, validation, release plan,
rollback plan, dry-run result, and logs) and produce human-readable
summary tables as well as a JSON-serializable report.
"""
from __future__ import annotations

import difflib
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from ..core.models import ExecutionSnapshot
from ..utils.storage import (
    LOG_FILE,
    get_run_dir,
)


MAX_LOG_LINES = 200
MAX_DIFF_CHARS = 500


@dataclass
class FieldDiff:
    field: str
    status: str
    a_value: Any = None
    b_value: Any = None
    details: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class ComponentDiff:
    name: str
    in_a: bool = False
    in_b: bool = False
    version_a: Optional[str] = None
    version_b: Optional[str] = None
    version_conflict: bool = False
    field_diffs: List[Dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class CompareReport:
    run_a: str
    run_b: str
    generated_at: str
    overview: Dict[str, Any] = field(default_factory=dict)
    config_diffs: List[FieldDiff] = field(default_factory=list)
    manifest_diffs: List[FieldDiff] = field(default_factory=list)
    component_diffs: List[ComponentDiff] = field(default_factory=list)
    validation_diffs: List[FieldDiff] = field(default_factory=list)
    release_plan_diffs: List[FieldDiff] = field(default_factory=list)
    rollback_plan_diffs: List[FieldDiff] = field(default_factory=list)
    dry_run_diffs: List[FieldDiff] = field(default_factory=list)
    log_diffs: Dict[str, Any] = field(default_factory=dict)
    warnings: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "run_a": self.run_a,
            "run_b": self.run_b,
            "generated_at": self.generated_at,
            "overview": self.overview,
            "config_diffs": [d.to_dict() for d in self.config_diffs],
            "manifest_diffs": [d.to_dict() for d in self.manifest_diffs],
            "component_diffs": [c.to_dict() for c in self.component_diffs],
            "validation_diffs": [d.to_dict() for d in self.validation_diffs],
            "release_plan_diffs": [d.to_dict() for d in self.release_plan_diffs],
            "rollback_plan_diffs": [d.to_dict() for d in self.rollback_plan_diffs],
            "dry_run_diffs": [d.to_dict() for d in self.dry_run_diffs],
            "log_diffs": self.log_diffs,
            "warnings": self.warnings,
        }


def _safe_get(d: Optional[Dict[str, Any]], key: str, default: Any = None) -> Any:
    if not isinstance(d, dict):
        return default
    return d.get(key, default)


def _truncate(text: str, max_chars: int = MAX_DIFF_CHARS) -> str:
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + f" ... (truncated, {len(text)} total chars)"


def _diff_scalars(field: str, a: Any, b: Any) -> FieldDiff:
    if a is None and b is None:
        return FieldDiff(field=field, status="both_missing")
    if a is None:
        return FieldDiff(field=field, status="only_in_b", b_value=b)
    if b is None:
        return FieldDiff(field=field, status="only_in_a", a_value=a)
    if a == b:
        return FieldDiff(field=field, status="same")
    return FieldDiff(
        field=field,
        status="different",
        a_value=a,
        b_value=b,
        details=f"{a!r} -> {b!r}",
    )


def _diff_dict_subset(
    a: Optional[Dict[str, Any]],
    b: Optional[Dict[str, Any]],
    keys: List[str],
    prefix: str = "",
) -> List[FieldDiff]:
    diffs: List[FieldDiff] = []
    for k in keys:
        f = f"{prefix}{k}" if prefix else k
        va = _safe_get(a, k)
        vb = _safe_get(b, k)
        diffs.append(_diff_scalars(f, va, vb))
    return diffs


def _diff_components(
    components_a: List[Dict[str, Any]],
    components_b: List[Dict[str, Any]],
) -> Tuple[List[ComponentDiff], List[str]]:
    warnings: List[str] = []
    a_by_name: Dict[str, List[Dict[str, Any]]] = {}
    b_by_name: Dict[str, List[Dict[str, Any]]] = {}

    for c in components_a:
        name = c.get("name", "unknown")
        a_by_name.setdefault(name, []).append(c)
    for c in components_b:
        name = c.get("name", "unknown")
        b_by_name.setdefault(name, []).append(c)

    all_names = sorted(set(a_by_name.keys()) | set(b_by_name.keys()))
    result: List[ComponentDiff] = []

    for name in all_names:
        a_list = a_by_name.get(name, [])
        b_list = b_by_name.get(name, [])
        cd = ComponentDiff(name=name)
        cd.in_a = len(a_list) > 0
        cd.in_b = len(b_list) > 0

        if len(a_list) > 1 or len(b_list) > 1:
            warnings.append(
                f"Component '{name}' appears {len(a_list)} time(s) in run_a "
                f"and {len(b_list)} time(s) in run_b - possible version conflict"
            )
            cd.version_conflict = True

        va = a_list[0].get("version") if a_list else None
        vb = b_list[0].get("version") if b_list else None
        cd.version_a = va
        cd.version_b = vb

        if cd.in_a and cd.in_b and va != vb:
            cd.version_conflict = True

        if cd.in_a and cd.in_b:
            ca, cb = a_list[0], b_list[0]
            for k in sorted(set(ca.keys()) | set(cb.keys())):
                if k in ("name",):
                    continue
                va_f = ca.get(k)
                vb_f = cb.get(k)
                if va_f != vb_f:
                    cd.field_diffs.append({
                        "field": k,
                        "a": va_f,
                        "b": vb_f,
                    })
        result.append(cd)
    return result, warnings


def _diff_plan(
    plan_a: Optional[Dict[str, Any]],
    plan_b: Optional[Dict[str, Any]],
    label: str,
) -> List[FieldDiff]:
    diffs: List[FieldDiff] = []
    if plan_a is None and plan_b is None:
        return [FieldDiff(field=f"{label}.present", status="both_missing")]
    if plan_a is None:
        return [FieldDiff(field=f"{label}.present", status="only_in_b",
                          b_value=f"{len(_safe_get(plan_b, 'steps', []))} steps")]
    if plan_b is None:
        return [FieldDiff(field=f"{label}.present", status="only_in_a",
                          a_value=f"{len(_safe_get(plan_a, 'steps', []))} steps")]

    diffs.extend(_diff_dict_subset(
        plan_a, plan_b,
        ["plan_id", "release_id", "target_environment", "total_estimated_minutes"],
        prefix=f"{label}.",
    ))

    order_a = _safe_get(plan_a, "execution_order", []) or []
    order_b = _safe_get(plan_b, "execution_order", []) or []
    if order_a != order_b:
        diffs.append(FieldDiff(
            field=f"{label}.execution_order",
            status="different",
            a_value=order_a,
            b_value=order_b,
            details=f"{' -> '.join(order_a)} vs {' -> '.join(order_b)}",
        ))

    steps_a = {s.get("component_name"): s for s in _safe_get(plan_a, "steps", [])}
    steps_b = {s.get("component_name"): s for s in _safe_get(plan_b, "steps", [])}
    all_names = sorted(set(steps_a.keys()) | set(steps_b.keys()))
    changed_steps = []
    for n in all_names:
        sa, sb = steps_a.get(n), steps_b.get(n)
        if sa is None or sb is None:
            continue
        for k in ("action", "component_version", "status", "approver"):
            if sa.get(k) != sb.get(k):
                changed_steps.append(f"{n}({sa.get(k)!r}->{sb.get(k)!r})")
                break
    if changed_steps:
        diffs.append(FieldDiff(
            field=f"{label}.steps",
            status="different",
            details="; ".join(changed_steps[:10]) + (" ..." if len(changed_steps) > 10 else ""),
        ))

    blocked_a = {b.get("component") for b in _safe_get(plan_a, "blocked_components", [])}
    blocked_b = {b.get("component") for b in _safe_get(plan_b, "blocked_components", [])}
    if blocked_a != blocked_b:
        diffs.append(FieldDiff(
            field=f"{label}.blocked_components",
            status="different",
            a_value=sorted(blocked_a),
            b_value=sorted(blocked_b),
        ))
    return diffs


def _diff_dry_run(
    dr_a: Optional[Dict[str, Any]],
    dr_b: Optional[Dict[str, Any]],
) -> List[FieldDiff]:
    diffs: List[FieldDiff] = []
    if dr_a is None and dr_b is None:
        return [FieldDiff(field="dry_run.present", status="both_missing")]
    if dr_a is None:
        return [FieldDiff(field="dry_run.present", status="only_in_b")]
    if dr_b is None:
        return [FieldDiff(field="dry_run.present", status="only_in_a")]

    sum_a = _safe_get(dr_a, "summary", {}) or {}
    sum_b = _safe_get(dr_b, "summary", {}) or {}
    diffs.extend(_diff_dict_subset(
        sum_a, sum_b,
        ["total_steps", "successful", "failed", "blocked", "skipped", "exit_code"],
        prefix="dry_run.summary.",
    ))

    steps_a = _safe_get(dr_a, "steps", []) or []
    steps_b = _safe_get(dr_b, "steps", []) or []
    a_statuses = {s.get("component_name"): s.get("status") for s in steps_a}
    b_statuses = {s.get("component_name"): s.get("status") for s in steps_b}
    all_names = sorted(set(a_statuses.keys()) | set(b_statuses.keys()))
    changed = []
    for n in all_names:
        if a_statuses.get(n) != b_statuses.get(n):
            changed.append(f"{n}:{a_statuses.get(n)}->{b_statuses.get(n)}")
    if changed:
        diffs.append(FieldDiff(
            field="dry_run.step_statuses",
            status="different",
            details="; ".join(changed[:10]) + (" ..." if len(changed) > 10 else ""),
        ))
    return diffs


def _diff_logs(
    snap_a: ExecutionSnapshot,
    snap_b: ExecutionSnapshot,
    run_a_id: str,
    run_b_id: str,
    base: Optional[str] = None,
) -> Dict[str, Any]:
    def _load_log_text(snap: ExecutionSnapshot, run_id: str) -> str:
        rd = get_run_dir(run_id, base)
        log_path = rd / LOG_FILE
        if log_path.exists():
            try:
                return log_path.read_text(encoding="utf-8", errors="replace")
            except Exception:
                pass
        logs = snap.logs or []
        lines = []
        for e in logs:
            extra = f" {e.get('extra', '')}" if e.get("extra") else ""
            lines.append(f"[{e.get('timestamp')}] [{e.get('level', ''):7s}] [{e.get('module', '')}] {e.get('message', '')}{extra}")
        return "\n".join(lines)

    text_a = _load_log_text(snap_a, run_a_id)
    text_b = _load_log_text(snap_b, run_b_id)
    lines_a = text_a.splitlines()
    lines_b = text_b.splitlines()

    truncated = False
    if len(lines_a) > MAX_LOG_LINES or len(lines_b) > MAX_LOG_LINES:
        truncated = True
        lines_a = lines_a[:MAX_LOG_LINES]
        lines_b = lines_b[:MAX_LOG_LINES]

    diff = list(difflib.unified_diff(
        lines_a, lines_b,
        fromfile=run_a_id, tofile=run_b_id,
        lineterm="", n=2,
    ))

    return {
        "lines_a_total": len(text_a.splitlines()),
        "lines_b_total": len(text_b.splitlines()),
        "lines_compared": max(len(lines_a), len(lines_b)),
        "truncated": truncated,
        "diff_lines": diff[:MAX_LOG_LINES],
        "diff_summary": {
            "added": sum(1 for ln in diff if ln.startswith("+") and not ln.startswith("+++")),
            "removed": sum(1 for ln in diff if ln.startswith("-") and not ln.startswith("---")),
        },
    }


def compare_snapshots(
    snap_a: ExecutionSnapshot,
    snap_b: ExecutionSnapshot,
    base: Optional[str] = None,
) -> CompareReport:
    """Compare two execution snapshots and produce a structured report."""
    from .models import now_iso

    report = CompareReport(
        run_a=snap_a.run_id,
        run_b=snap_b.run_id,
        generated_at=now_iso(),
    )

    report.overview = {
        "command_a": snap_a.command,
        "command_b": snap_b.command,
        "exit_code_a": snap_a.exit_code,
        "exit_code_b": snap_b.exit_code,
        "started_at_a": snap_a.started_at,
        "started_at_b": snap_b.started_at,
        "finished_at_a": snap_a.finished_at,
        "finished_at_b": snap_b.finished_at,
    }

    report.config_diffs = _diff_dict_subset(
        snap_a.config_snapshot, snap_b.config_snapshot,
        ["command"],
        prefix="config.",
    )
    args_a = _safe_get(snap_a.config_snapshot, "args", {}) or {}
    args_b = _safe_get(snap_b.config_snapshot, "args", {}) or {}
    report.config_diffs.extend(_diff_dict_subset(
        args_a, args_b,
        ["manifest", "output", "format", "no_dryrun", "seed", "command", "limit", "show"],
        prefix="config.args.",
    ))

    ms_a = snap_a.manifest_snapshot
    ms_b = snap_b.manifest_snapshot
    if ms_a is None and ms_b is None:
        report.manifest_diffs.append(FieldDiff(field="manifest.present", status="both_missing"))
    elif ms_a is None:
        report.manifest_diffs.append(FieldDiff(field="manifest.present", status="only_in_b"))
        report.warnings.append(f"Run {snap_a.run_id} has no manifest snapshot")
    elif ms_b is None:
        report.manifest_diffs.append(FieldDiff(field="manifest.present", status="only_in_a"))
        report.warnings.append(f"Run {snap_b.run_id} has no manifest snapshot")
    else:
        report.manifest_diffs.extend(_diff_dict_subset(
            ms_a, ms_b,
            ["manifest_version", "release_id", "title", "target_environment", "scheduled_date", "created_by"],
            prefix="manifest.",
        ))
        comp_a = _safe_get(ms_a, "components", []) or []
        comp_b = _safe_get(ms_b, "components", []) or []
        comp_diffs, comp_warnings = _diff_components(comp_a, comp_b)
        report.component_diffs = comp_diffs
        report.warnings.extend(comp_warnings)

    vr_a = snap_a.validation_result
    vr_b = snap_b.validation_result
    if vr_a is None and vr_b is None:
        report.validation_diffs.append(FieldDiff(field="validation.present", status="both_missing"))
    elif vr_a is None:
        report.validation_diffs.append(FieldDiff(field="validation.present", status="only_in_b"))
        report.warnings.append(f"Run {snap_a.run_id} has no validation result")
    elif vr_b is None:
        report.validation_diffs.append(FieldDiff(field="validation.present", status="only_in_a"))
        report.warnings.append(f"Run {snap_b.run_id} has no validation result")
    else:
        report.validation_diffs.append(_diff_scalars("validation.passed", _safe_get(vr_a, "passed"), _safe_get(vr_b, "passed")))
        sum_a = _safe_get(vr_a, "summary", {}) or {}
        sum_b = _safe_get(vr_b, "summary", {}) or {}
        report.validation_diffs.extend(_diff_dict_subset(
            sum_a, sum_b,
            ["total", "errors", "warnings", "infos"],
            prefix="validation.summary.",
        ))
        issues_a = _safe_get(vr_a, "issues", []) or []
        issues_b = _safe_get(vr_b, "issues", []) or []
        codes_a = {i.get("issue_code") for i in issues_a}
        codes_b = {i.get("issue_code") for i in issues_b}
        if codes_a != codes_b:
            report.validation_diffs.append(FieldDiff(
                field="validation.issue_codes",
                status="different",
                a_value=sorted(codes_a),
                b_value=sorted(codes_b),
            ))

    report.release_plan_diffs = _diff_plan(snap_a.release_plan, snap_b.release_plan, "release_plan")
    if snap_a.release_plan is None:
        report.warnings.append(f"Run {snap_a.run_id} has no release plan")
    if snap_b.release_plan is None:
        report.warnings.append(f"Run {snap_b.run_id} has no release plan")

    report.rollback_plan_diffs = _diff_plan(snap_a.rollback_plan, snap_b.rollback_plan, "rollback_plan")
    if snap_a.rollback_plan is None:
        report.warnings.append(f"Run {snap_a.run_id} has no rollback plan")
    if snap_b.rollback_plan is None:
        report.warnings.append(f"Run {snap_b.run_id} has no rollback plan")

    report.dry_run_diffs = _diff_dry_run(snap_a.dry_run_result, snap_b.dry_run_result)
    if snap_a.dry_run_result is None:
        report.warnings.append(f"Run {snap_a.run_id} has no dry-run result")
    if snap_b.dry_run_result is None:
        report.warnings.append(f"Run {snap_b.run_id} has no dry-run result")

    report.log_diffs = _diff_logs(snap_a, snap_b, snap_a.run_id, snap_b.run_id, base)

    report.warnings = sorted(set(report.warnings))
    return report


def format_diff_table(diffs: List[FieldDiff], title: str) -> str:
    """Format a list of FieldDiffs as a readable table."""
    lines: List[str] = []
    lines.append(f"\n--- {title} ---")
    lines.append(f"{'Status':14s}  {'Field':40s}  Details")
    lines.append("-" * 100)
    shown = 0
    for d in diffs:
        if d.status == "same":
            continue
        shown += 1
        details = d.details
        if not details and d.status in ("only_in_a", "only_in_b", "different"):
            if d.a_value is not None:
                details = f"A={_truncate(repr(d.a_value), 80)}"
            if d.b_value is not None:
                if details:
                    details += " | "
                details += f"B={_truncate(repr(d.b_value), 80)}"
        lines.append(f"{d.status:14s}  {d.field:40s}  {details}")
    if shown == 0:
        lines.append("  (no differences)")
    return "\n".join(lines)


def format_component_table(cds: List[ComponentDiff]) -> str:
    lines: List[str] = []
    lines.append("\n--- Component Differences ---")
    lines.append(f"{'Component':25s}  {'In A':5s}  {'In B':5s}  {'Ver A':12s}  {'Ver B':12s}  {'Conflict':8s}  Changed Fields")
    lines.append("-" * 120)
    for c in cds:
        if c.in_a and c.in_b and c.version_a == c.version_b and not c.field_diffs:
            continue
        changed_fields = ", ".join(f"{fd['field']}" for fd in c.field_diffs[:5])
        if len(c.field_diffs) > 5:
            changed_fields += f" ...(+{len(c.field_diffs) - 5})"
        lines.append(
            f"{c.name:25s}  "
            f"{'YES' if c.in_a else 'NO':5s}  "
            f"{'YES' if c.in_b else 'NO':5s}  "
            f"{(c.version_a or '-'):12s}  "
            f"{(c.version_b or '-'):12s}  "
            f"{'YES' if c.version_conflict else '':8s}  "
            f"{changed_fields}"
        )
    return "\n".join(lines)


def format_log_table(log_diffs: Dict[str, Any]) -> str:
    lines: List[str] = []
    lines.append("\n--- Log Differences ---")
    lines.append(f"  Lines in A: {log_diffs.get('lines_a_total', 0)}")
    lines.append(f"  Lines in B: {log_diffs.get('lines_b_total', 0)}")
    lines.append(f"  Lines compared: {log_diffs.get('lines_compared', 0)}"
                 f"{' (truncated)' if log_diffs.get('truncated') else ''}")
    ds = log_diffs.get("diff_summary", {})
    lines.append(f"  Diff summary: +{ds.get('added', 0)} added, -{ds.get('removed', 0)} removed")
    diff_lines = log_diffs.get("diff_lines", [])
    if diff_lines:
        lines.append("  Diff preview (first 30 lines):")
        for dl in diff_lines[:30]:
            lines.append(f"    {dl}")
        if len(diff_lines) > 30:
            lines.append(f"    ... ({len(diff_lines) - 30} more lines)")
    else:
        lines.append("  (no line-level differences in compared portion)")
    return "\n".join(lines)


def format_report_text(report: CompareReport) -> str:
    """Format a CompareReport as human-readable text with tables."""
    from ..utils.exit_codes import get_exit_code_by_code

    lines: List[str] = []
    lines.append("=" * 100)
    lines.append(f"  RUN SNAPSHOT COMPARISON")
    lines.append(f"  Run A: {report.run_a}")
    lines.append(f"  Run B: {report.run_b}")
    lines.append(f"  Generated: {report.generated_at}")
    lines.append("=" * 100)

    ov = report.overview
    ec_a = get_exit_code_by_code(int(ov.get("exit_code_a", 0)))
    ec_b = get_exit_code_by_code(int(ov.get("exit_code_b", 0)))
    lines.append(f"\n--- Overview ---")
    lines.append(f"{'Attribute':25s}  {'Run A':50s}  {'Run B':50s}")
    lines.append("-" * 130)
    ec_a_str = f"{ov.get('exit_code_a', '?')} ({ec_a.name})"
    ec_b_str = f"{ov.get('exit_code_b', '?')} ({ec_b.name})"
    lines.append(f"{'Command':25s}  {str(ov.get('command_a', '?')):50s}  {str(ov.get('command_b', '?')):50s}")
    lines.append(f"{'Exit Code':25s}  {ec_a_str:50s}  {ec_b_str:50s}")
    lines.append(f"{'Started':25s}  {str(ov.get('started_at_a', '?')):50s}  {str(ov.get('started_at_b', '?')):50s}")
    lines.append(f"{'Finished':25s}  {str(ov.get('finished_at_a', '?') or '(incomplete)'):50s}  {str(ov.get('finished_at_b', '?') or '(incomplete)'):50s}")

    if report.warnings:
        lines.append(f"\n--- Warnings ({len(report.warnings)}) ---")
        for w in report.warnings:
            lines.append(f"  ! {w}")

    lines.append(format_diff_table(report.config_diffs, "Config Differences"))
    lines.append(format_diff_table(report.manifest_diffs, "Manifest Differences"))
    if report.component_diffs:
        lines.append(format_component_table(report.component_diffs))
    lines.append(format_diff_table(report.validation_diffs, "Validation Differences"))
    lines.append(format_diff_table(report.release_plan_diffs, "Release Plan Differences"))
    lines.append(format_diff_table(report.rollback_plan_diffs, "Rollback Plan Differences"))
    lines.append(format_diff_table(report.dry_run_diffs, "Dry-Run Result Differences"))
    lines.append(format_log_table(report.log_diffs))

    lines.append("\n" + "=" * 100)
    return "\n".join(lines)
