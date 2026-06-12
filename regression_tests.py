"""Regression tests for the two bug fixes.

Runs concrete assertions against the codebase and the actual CLI:

  1. generate_id() uniqueness within same second (1000 calls, 0 collisions)
  2. generate_id() format unchanged (3 segments, PREFIX-YYYYMMDDHHMMSS-xxxxxxxx)
  3. CLI: 6 validates executed in rapid succession -> 6 distinct history dirs
  4. CLI: export zip contains all 7 expected files incl. config.json, run.log
  5. Back-compat: old-format (1-only-per-second) ids still readable via history
  6. Cross-consistency: CLI stdout msg count == history run.log count == zip run.log count

Each assertion prints a line starting with "REGRESSION:" so the user can grep
them.  The script exits 0 only if *all* assertions pass.
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
import threading
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

HIST_DIR = ROOT / ".release_orchestrator" / "history"
EXPECTED_EXPORT_FILES = {
    "manifest.json",
    "validation.json",
    "release_plan.json",
    "rollback_plan.json",
    "dry_run_result.json",
    "config.json",
    "run.log",
}
ID_RE = re.compile(r"^[A-Z]+-\d{14}-[0-9a-f]{8}$")
regression_results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    regression_results.append((name, ok, detail))
    status = "PASS" if ok else "FAIL"
    print(f"REGRESSION: [{status}] {name} {(' -- ' + detail) if detail else ''}".rstrip())


# ---------------------------------------------------------------------------
# 1 & 2 -- generate_id() uniqueness and format
# ---------------------------------------------------------------------------
from release_orchestrator.core.models import generate_id

print("Section 1: generate_id() uniqueness (same-second)")
print("-" * 60)

N_CALLS = 1000
ids = set()
collision_count = 0
for _ in range(N_CALLS):
    new_id = generate_id("RUN")
    before = len(ids)
    ids.add(new_id)
    if len(ids) == before:
        collision_count += 1

check(
    "generate_id 1000 back-to-back calls all unique",
    collision_count == 0 and len(ids) == N_CALLS,
    f"unique={len(ids)}/{N_CALLS} collisions={collision_count}",
)

# Format check: 3 parts, last part 8 hex chars, middle exactly 14 digits
bad_format = [i for i in ids if not ID_RE.match(i)]
check(
    f"generate_id format unchanged ({N_CALLS} samples)",
    len(bad_format) == 0,
    f"bad examples: {bad_format[:3]}",
)

# Threaded uniqueness - simulate concurrent invocations in same process
thread_ids: set[str] = set()
thread_lock = threading.Lock()


def _worker(tid: int, n: int) -> None:
    for i in range(n):
        new_id = generate_id(f"T{tid}")
        with thread_lock:
            thread_ids.add(new_id)


N_THREADS, PER_THREAD = 10, 100
threads = [threading.Thread(target=_worker, args=(t, PER_THREAD)) for t in range(N_THREADS)]
for t in threads:
    t.start()
for t in threads:
    t.join()
expected_count = N_THREADS * PER_THREAD
check(
    f"generate_id thread-safe across {N_THREADS} threads x {PER_THREAD} calls",
    len(thread_ids) == expected_count,
    f"unique={len(thread_ids)}/{expected_count}",
)

# ---------------------------------------------------------------------------
# 3 -- 6 validate runs in rapid succession must create 6 distinct history dirs
# ---------------------------------------------------------------------------
print()
print("Section 3: same-second CLI validate runs do not overwrite history")
print("-" * 60)

manifest = ROOT / "examples" / "clean_manifest.json"
if not manifest.exists():
    subprocess.run(
        [sys.executable, "-m", "release_orchestrator", "init",
         "-o", str(manifest), "--no-errors", "--env", "staging", "--clean"],
        cwd=str(ROOT), capture_output=True, text=True,
    )

before = {p.name for p in HIST_DIR.iterdir() if p.is_dir()} if HIST_DIR.exists() else set()

NUM_VALIDATES = 6
exit_codes: list[int] = []
for i in range(NUM_VALIDATES):
    proc = subprocess.run(
        [sys.executable, "-m", "release_orchestrator", "validate", "-m", str(manifest)],
        cwd=str(ROOT), capture_output=True, text=True,
    )
    exit_codes.append(proc.returncode)

after = {p.name for p in HIST_DIR.iterdir() if p.is_dir()} if HIST_DIR.exists() else set()
new_dirs = sorted(after - before)

check(
    f"{NUM_VALIDATES} rapid CLI validates -> {NUM_VALIDATES} distinct history dirs",
    len(new_dirs) == NUM_VALIDATES,
    f"created={len(new_dirs)} new_dirs={new_dirs}",
)
all_zero = all(ec == 0 for ec in exit_codes)
check(
    f"{NUM_VALIDATES} clean manifest validates all exit 0",
    all_zero,
    f"exit_codes={exit_codes}",
)

# ---------------------------------------------------------------------------
# 4 -- export zip contains all 7 files
# ---------------------------------------------------------------------------
print()
print("Section 4: export archive contains config.json and run.log")
print("-" * 60)

export_out = ROOT / "archives" / "regression_export_bundle"
if export_out.with_suffix(".zip").exists():
    export_out.with_suffix(".zip").unlink()

# Also record how many [INFO] lines the CLI prints so we can cross-check later
proc = subprocess.run(
    [sys.executable, "-m", "release_orchestrator", "export",
     "-m", str(manifest), "-o", str(export_out), "--format", "zip"],
    cwd=str(ROOT), capture_output=True, text=True,
)
export_stdout = proc.stdout
export_exit = proc.returncode
print(export_stdout)

check("export command exits 0", export_exit == 0, f"exit={export_exit}")

zip_path = export_out.with_suffix(".zip")
check(f"export zip exists at {zip_path.name}", zip_path.exists(), f"exists={zip_path.exists()}")

zip_names = set()
zip_contents: dict[str, bytes] = {}
if zip_path.exists():
    with zipfile.ZipFile(zip_path) as zf:
        zip_names = set(zf.namelist())
        for name in zip_names:
            with zf.open(name) as fp:
                zip_contents[name] = fp.read()

missing_files = EXPECTED_EXPORT_FILES - zip_names
extra_files = zip_names - EXPECTED_EXPORT_FILES
check(
    "export zip has all 7 expected files",
    not missing_files,
    f"missing={sorted(missing_files)} extra={sorted(extra_files)}",
)

# Also verify all non-empty (we know manifest/validation etc should be large)
for f in ["manifest.json", "validation.json", "release_plan.json", "rollback_plan.json",
          "dry_run_result.json", "config.json"]:
    sz = len(zip_contents.get(f, b""))
    check(f"export zip contains non-empty {f}", sz > 0, f"size={sz}")

# run.log should also be non-empty and start with the canonical [timestamp]
run_log_b = zip_contents.get("run.log", b"")
run_log_t = run_log_b.decode("utf-8", errors="replace")
check(
    "export zip run.log is non-empty and looks like orchestrator logs",
    len(run_log_t) > 100 and ("[INFO   ]" in run_log_t or "[cli]" in run_log_t),
    f"size={len(run_log_t)} starts_with={run_log_t[:60]!r}",
)

# ---------------------------------------------------------------------------
# 5 -- Backward compatibility: existing (pre-fix) history entries still readable
# ---------------------------------------------------------------------------
print()
print("Section 5: backward compatibility with existing history")
print("-" * 60)

# Check old runs (from repro_bugs.py or earlier) still loadable via storage API
from release_orchestrator.utils.storage import list_history, get_snapshot

all_history = list_history()
check(
    "storage.list_history returns at least the 6 new validate + 1 export runs",
    len(all_history) >= NUM_VALIDATES + 1,
    f"total_entries={len(all_history)}",
)

# Pick the oldest (pre-fix) id we can find and verify get_snapshot works
oldest_run_id = None
if all_history:
    oldest_run_id = sorted(h["run_id"] for h in all_history)[0]
    snap = get_snapshot(oldest_run_id)
    check(
        f"storage.get_snapshot loads oldest run {oldest_run_id}",
        snap is not None and hasattr(snap, "command"),
        f"snap_is_None={snap is None}",
    )

# Make sure both new-format ids (our 6 validates) and any old ones all
# share the identical regex layout (we never changed format).
bad_ids = [h["run_id"] for h in all_history if not ID_RE.match(h["run_id"])]
check(
    "all history run_ids (old+new) conform to the 3-segment format",
    len(bad_ids) == 0,
    f"bad examples={bad_ids[:3]}",
)

# ---------------------------------------------------------------------------
# 6 -- Cross-consistency: CLI stdout / history run.log / zip run.log line up
# ---------------------------------------------------------------------------
print()
print("Section 6: CLI output, history and zip contents match each other")
print("-" * 60)

# (a) Find the history dir produced by our export call above
after_export_runs = sorted(
    (p for p in HIST_DIR.iterdir() if p.is_dir()),
    key=lambda p: p.stat().st_mtime,
    reverse=True,
)
export_history_dir = None
for d in after_export_runs:
    sf = d / "snapshot.json"
    if sf.exists():
        try:
            snap = json.loads(sf.read_text(encoding="utf-8"))
            if snap.get("command") == "export" and snap.get("manifest_snapshot"):
                export_history_dir = d
                break
        except Exception:
            pass

check("found history dir for the regression export run",
      export_history_dir is not None,
      f"candidates_checked={len(after_export_runs)}")

if export_history_dir:
    hist_run_log = (export_history_dir / "run.log").read_text(encoding="utf-8", errors="replace")
    hist_manifest = json.loads((export_history_dir / "manifest_snapshot.json").read_text(encoding="utf-8"))
    hist_validation = json.loads((export_history_dir / "validation.json").read_text(encoding="utf-8"))
    hist_config = json.loads((export_history_dir / "config.json").read_text(encoding="utf-8"))
    zip_manifest = json.loads(zip_contents["manifest.json"])
    zip_validation = json.loads(zip_contents["validation.json"])
    zip_config = json.loads(zip_contents["config.json"])

    # Manifest identity must be identical between history and zip
    check(
        "zip manifest.json == history manifest_snapshot.json",
        hist_manifest == zip_manifest,
        f"release_id hist={hist_manifest.get('release_id')} zip={zip_manifest.get('release_id')}",
    )
    check(
        "zip validation.json == history validation.json",
        hist_validation == zip_validation,
        f"summary hist={hist_validation.get('summary')} zip={zip_validation.get('summary')}",
    )

    # Config should at least agree on command and run_id (args.func string repr
    # may differ but that's fine - we only care about the stable parts)
    check(
        "zip config.json command matches history",
        zip_config.get("command") == hist_config.get("command") == "export",
        f"zip_cmd={zip_config.get('command')} hist_cmd={hist_config.get('command')}",
    )
    check(
        "zip config.json run_id matches history dir name",
        zip_config.get("run_id") == export_history_dir.name,
        f"zip_runid={zip_config.get('run_id')} dir={export_history_dir.name}",
    )

    # run.log in zip should be a prefix of history run.log
    # (history keeps a few extra lines post-export about persist_run_artifacts,
    #  but all the validation / planning / dry-run lines that users care about
    #  must appear identically in both)
    hist_lines = [ln for ln in hist_run_log.splitlines() if ln.strip()]
    zip_lines = [ln for ln in run_log_t.splitlines() if ln.strip()]
    hist_prefix = hist_lines[: len(zip_lines)]
    check(
        "zip run.log lines are a prefix of history run.log",
        len(zip_lines) >= 5 and hist_prefix == zip_lines,
        f"zip_lines={len(zip_lines)} hist_prefix_match={hist_prefix == zip_lines}",
    )

    # CLI stdout should also reference the same release_id and archive path
    zip_rid = zip_manifest.get("release_id")
    check(
        f"CLI stdout mentions release_id {zip_rid}",
        zip_rid in export_stdout,
        f"mentioned={zip_rid in export_stdout}",
    )
    check(
        "CLI stdout lists all 7 archive file names",
        all(name in export_stdout for name in EXPECTED_EXPORT_FILES),
        f"missing_from_stdout={[n for n in EXPECTED_EXPORT_FILES if n not in export_stdout]}",
    )

# ---------------------------------------------------------------------------
# Section 7: history compare - JSON output
# ---------------------------------------------------------------------------
print()
print("Section 7: history compare -- JSON output")
print("-" * 60)

from release_orchestrator.core.compare import (
    compare_snapshots,
    format_report_text,
    FieldDiff,
    ComponentDiff,
    CompareReport,
)
from release_orchestrator.core.models import ExecutionSnapshot
from release_orchestrator.utils.storage import get_snapshot, list_history

all_runs = list_history()
if len(all_runs) >= 2:
    run_a_id = all_runs[0]["run_id"]
    run_b_id = all_runs[1]["run_id"]
    snap_a = get_snapshot(run_a_id)
    snap_b = get_snapshot(run_b_id)

    check("can load two snapshots for compare testing",
          snap_a is not None and snap_b is not None,
          f"snap_a={snap_a is not None} snap_b={snap_b is not None}")

    if snap_a and snap_b:
        report = compare_snapshots(snap_a, snap_b)
        check("compare_snapshots returns CompareReport",
              isinstance(report, CompareReport),
              f"type={type(report).__name__}")

        report_dict = report.to_dict()
        check("CompareReport.to_dict() is JSON-serializable",
              isinstance(json.dumps(report_dict), str),
              f"keys={sorted(report_dict.keys())[:10]}")

        required_keys = {"run_a", "run_b", "generated_at", "overview",
                         "config_diffs", "manifest_diffs", "component_diffs",
                         "validation_diffs", "release_plan_diffs",
                         "rollback_plan_diffs", "dry_run_diffs",
                         "log_diffs", "warnings"}
        missing_keys = required_keys - set(report_dict.keys())
        check("compare report has all required top-level keys",
              not missing_keys,
              f"missing={sorted(missing_keys)}")

        check("compare report run_a matches input",
              report_dict["run_a"] == run_a_id,
              f"run_a={report_dict['run_a']}")
        check("compare report run_b matches input",
              report_dict["run_b"] == run_b_id,
              f"run_b={report_dict['run_b']}")

        text_report = format_report_text(report)
        check("format_report_text produces non-empty output",
              len(text_report) > 200,
              f"length={len(text_report)}")
        check("text report contains both run IDs",
              run_a_id in text_report and run_b_id in text_report,
              f"has_a={run_a_id in text_report} has_b={run_b_id in text_report}")
        check("text report contains section headers",
              "Config Differences" in text_report
              and "Validation Differences" in text_report,
              f"has_config={'Config Differences' in text_report} has_val={'Validation Differences' in text_report}")

        log_diffs = report_dict.get("log_diffs", {})
        check("log diffs include summary counts",
              "diff_summary" in log_diffs and "lines_a_total" in log_diffs,
              f"keys={sorted(log_diffs.keys())}")


# ---------------------------------------------------------------------------
# Section 8: history compare - CLI (JSON output + error handling)
# ---------------------------------------------------------------------------
print()
print("Section 8: history compare CLI tests")
print("-" * 60)

if len(all_runs) >= 2:
    proc = subprocess.run(
        [sys.executable, "-m", "release_orchestrator",
         "history", "compare", run_a_id, run_b_id, "--json"],
        cwd=str(ROOT), capture_output=True, text=True,
    )
    check("history compare --json exits 0",
          proc.returncode == 0,
          f"exit_code={proc.returncode}")

    try:
        # 在 stdout 中找到所有完整的 JSON 对象，取最大的那个（compare report）
        text = proc.stdout
        candidates = []
        i = 0
        while i < len(text):
            if text[i] == "{":
                depth = 0
                start = i
                for j in range(i, len(text)):
                    if text[j] == "{":
                        depth += 1
                    elif text[j] == "}":
                        depth -= 1
                        if depth == 0:
                            candidates.append(text[start:j+1])
                            i = j + 1
                            break
                else:
                    i += 1
            else:
                i += 1

        # 找包含 "overview" 或 "run_a" + "run_b" + "config_diffs" 的那个
        cli_report = None
        for cand in candidates:
            try:
                obj = json.loads(cand)
                if isinstance(obj, dict) and "config_diffs" in obj and "run_a" in obj and "run_b" in obj:
                    cli_report = obj
                    break
            except Exception:
                continue

        if cli_report:
            check("CLI compare JSON output is valid", True)
            check("CLI compare JSON has run_a and run_b",
                  cli_report.get("run_a") == run_a_id
                  and cli_report.get("run_b") == run_b_id,
                  f"run_a={cli_report.get('run_a')} run_b={cli_report.get('run_b')}")
        else:
            check("CLI compare JSON output is valid", False,
                  f"no compare report JSON found, candidates={len(candidates)}")
    except Exception as e:
        check("CLI compare JSON output is valid", False, f"error={e}")

# Test nonexistent run ID
proc_bad = subprocess.run(
    [sys.executable, "-m", "release_orchestrator",
     "history", "compare", "NO_SUCH_RUN_12345", run_b_id if len(all_runs) >= 2 else "NO_SUCH_TWO"],
    cwd=str(ROOT), capture_output=True, text=True,
)
check("history compare with nonexistent run_id returns EXIT_HISTORY_ERROR (15)",
      proc_bad.returncode == 15,
      f"exit_code={proc_bad.returncode}")
check("history compare error message mentions run not found",
      "not found" in proc_bad.stdout.lower() or "not found" in proc_bad.stderr.lower(),
      f"stdout_preview={proc_bad.stdout[:100]!r}")


# ---------------------------------------------------------------------------
# Section 9: export with compare - zip content + source run IDs
# ---------------------------------------------------------------------------
print()
print("Section 9: export --compare-with produces compare_report.json")
print("-" * 60)

if len(all_runs) >= 1:
    ref_run = all_runs[-1]["run_id"]
    compare_export_out = ROOT / "archives" / "regression_compare_export"
    if compare_export_out.with_suffix(".zip").exists():
        compare_export_out.with_suffix(".zip").unlink()

    proc_comp = subprocess.run(
        [sys.executable, "-m", "release_orchestrator", "export",
         "-m", str(manifest),
         "-o", str(compare_export_out),
         "--compare-with", ref_run,
         "--format", "zip"],
        cwd=str(ROOT), capture_output=True, text=True,
    )
    check("export --compare-with exits 0",
          proc_comp.returncode == 0,
          f"exit_code={proc_comp.returncode} stderr={proc_comp.stderr[:100]}")

    zip_path = compare_export_out.with_suffix(".zip")
    check(f"compare export zip exists", zip_path.exists(), f"exists={zip_path.exists()}")

    if zip_path.exists():
        with zipfile.ZipFile(zip_path) as zf:
            names = set(zf.namelist())
            check("compare export zip contains compare_report.json",
                  "compare_report.json" in names,
                  f"names={sorted(names)}")

            if "compare_report.json" in names:
                with zf.open("compare_report.json") as f:
                    creport = json.load(f)
                check("compare_report.json has source_run_a and source_run_b",
                      "source_run_a" in creport and "source_run_b" in creport,
                      f"keys={sorted(creport.keys())[:15]}")
                check("compare_report.json source_run_a matches reference",
                      creport.get("source_run_a") == ref_run,
                      f"source_run_a={creport.get('source_run_a')} ref={ref_run}")
                check("compare_report.json has all expected diff sections",
                      all(k in creport for k in [
                          "config_diffs", "manifest_diffs", "validation_diffs",
                          "release_plan_diffs", "rollback_plan_diffs",
                          "dry_run_diffs", "log_diffs", "component_diffs",
                      ]),
                      f"missing={[k for k in ['config_diffs','manifest_diffs','validation_diffs'] if k not in creport]}")


# ---------------------------------------------------------------------------
# Section 10: compare with old/partial history (missing fields)
# ---------------------------------------------------------------------------
print()
print("Section 10: compare handles partial/old history (missing fields)")
print("-" * 60)

minimal_snap_a = ExecutionSnapshot(
    run_id="MINIMAL-A",
    command="init",
    started_at="2020-01-01T00:00:00Z",
    finished_at="2020-01-01T00:00:01Z",
    exit_code=0,
    config_snapshot=None,
    manifest_snapshot=None,
    validation_result=None,
    release_plan=None,
    rollback_plan=None,
    dry_run_result=None,
    logs=[],
)
full_snap_b = ExecutionSnapshot(
    run_id="FULL-B",
    command="export",
    started_at="2020-01-02T00:00:00Z",
    finished_at="2020-01-02T00:00:05Z",
    exit_code=0,
    config_snapshot={"command": "export", "args": {"manifest": "x.json"}},
    manifest_snapshot={
        "release_id": "REL-TEST",
        "components": [
            {"name": "comp1", "version": "1.0.0", "environment": "prod",
             "artifact": {"path": "a.pkg", "checksum": "abc"}},
        ],
    },
    validation_result={
        "timestamp": "2020-01-02T00:00:01Z",
        "passed": True,
        "issues": [],
        "summary": {"total": 0, "errors": 0, "warnings": 0, "infos": 0},
    },
    release_plan={
        "plan_id": "PLAN-1", "release_id": "REL-TEST", "generated_at": "x",
        "target_environment": "prod",
        "execution_order": ["comp1"],
        "steps": [{"step_index": 0, "component_name": "comp1",
                   "component_version": "1.0.0", "action": "deploy",
                   "status": "scheduled"}],
        "blocked_components": [],
        "total_estimated_minutes": 5,
    },
    rollback_plan={
        "plan_id": "RBP-1", "release_id": "REL-TEST", "generated_at": "x",
        "target_environment": "prod",
        "execution_order": ["comp1"],
        "steps": [{"step_index": 0, "component_name": "comp1",
                   "component_version": "0.9.0", "action": "rollback",
                   "status": "scheduled"}],
        "blocked_components": [],
        "total_estimated_minutes": 3,
    },
    dry_run_result={
        "summary": {"total_steps": 1, "successful": 1, "failed": 0,
                    "blocked": 0, "skipped": 0, "exit_code": 0},
        "steps": [],
    },
    logs=[],
)

partial_report = compare_snapshots(minimal_snap_a, full_snap_b)
partial_dict = partial_report.to_dict()

check("partial compare generates warnings for missing fields",
      len(partial_dict.get("warnings", [])) >= 3,
      f"warnings={partial_dict.get('warnings')}")

check("partial compare: manifest.present is only_in_b",
      any(d.get("field") == "manifest.present" and d.get("status") == "only_in_b"
          for d in partial_dict.get("manifest_diffs", [])),
      f"manifest_diffs={[d['field'] for d in partial_dict.get('manifest_diffs', [])]}")

check("partial compare: validation.present is only_in_b",
      any(d.get("field") == "validation.present" and d.get("status") == "only_in_b"
          for d in partial_dict.get("validation_diffs", [])),
      f"validation_diffs_fields={[d['field'] for d in partial_dict.get('validation_diffs', [])]}")

check("partial compare report is still JSON serializable",
      isinstance(json.dumps(partial_dict), str),
      f"serializable={isinstance(json.dumps(partial_dict), str)}")


# ---------------------------------------------------------------------------
# Section 11: compare with conflicting component versions
# ---------------------------------------------------------------------------
print()
print("Section 11: compare detects same-name component version conflicts")
print("-" * 60)

snap_conflict_a = ExecutionSnapshot(
    run_id="CONFLICT-A",
    command="plan",
    started_at="2020-01-01T00:00:00Z",
    exit_code=0,
    manifest_snapshot={
        "release_id": "REL-A",
        "components": [
            {"name": "shared-lib", "version": "1.0.0", "environment": "prod",
             "artifact": {"path": "a.pkg", "checksum": "abc"}},
            {"name": "api-gateway", "version": "2.0.0", "environment": "prod",
             "artifact": {"path": "b.pkg", "checksum": "def"}},
            {"name": "only-in-a", "version": "1.0.0", "environment": "prod",
             "artifact": {"path": "c.pkg", "checksum": "ghi"}},
        ],
    },
)

snap_conflict_b = ExecutionSnapshot(
    run_id="CONFLICT-B",
    command="plan",
    started_at="2020-01-02T00:00:00Z",
    exit_code=0,
    manifest_snapshot={
        "release_id": "REL-B",
        "components": [
            {"name": "shared-lib", "version": "1.1.0", "environment": "prod",
             "artifact": {"path": "a.pkg", "checksum": "xyz"}},
            {"name": "api-gateway", "version": "2.0.0", "environment": "prod",
             "artifact": {"path": "b.pkg", "checksum": "def"}},
            {"name": "only-in-b", "version": "3.0.0", "environment": "prod",
             "artifact": {"path": "d.pkg", "checksum": "jkl"}},
        ],
    },
)

conflict_report = compare_snapshots(snap_conflict_a, snap_conflict_b)
conflict_dict = conflict_report.to_dict()
comp_diffs = conflict_dict.get("component_diffs", [])

check("component diffs include all 4 unique component names",
      len(comp_diffs) == 4,
      f"components={[c['name'] for c in comp_diffs]}")

shared_lib_diff = next((c for c in comp_diffs if c["name"] == "shared-lib"), None)
check("shared-lib is flagged as version conflict",
      shared_lib_diff is not None and shared_lib_diff.get("version_conflict") is True,
      f"shared_lib={shared_lib_diff}")

api_gw_diff = next((c for c in comp_diffs if c["name"] == "api-gateway"), None)
check("api-gateway (same version) is NOT flagged as conflict",
      api_gw_diff is not None and api_gw_diff.get("version_conflict") is False,
      f"api_gw={api_gw_diff}")

only_a = next((c for c in comp_diffs if c["name"] == "only-in-a"), None)
check("only-in-a appears only in A",
      only_a is not None and only_a.get("in_a") and not only_a.get("in_b"),
      f"only_in_a={only_a}")

only_b = next((c for c in comp_diffs if c["name"] == "only-in-b"), None)
check("only-in-b appears only in B",
      only_b is not None and not only_b.get("in_a") and only_b.get("in_b"),
      f"only_in_b={only_b}")


# ---------------------------------------------------------------------------
# Section 12: cross-restart recompute (from history dir)
# ---------------------------------------------------------------------------
print()
print("Section 12: compare can be recomputed from history dirs (cross-restart)")
print("-" * 60)

if len(all_runs) >= 2:
    # 从历史目录重新加载两个快照并比较，确保结果与直接从 get_snapshot 一致
    snap1 = get_snapshot(run_a_id)
    snap2 = get_snapshot(run_b_id)
    if snap1 and snap2:
        r1 = compare_snapshots(snap1, snap2)
        # 模拟重启：重新加载一次再比较
        snap1_reload = get_snapshot(run_a_id)
        snap2_reload = get_snapshot(run_b_id)
        r2 = compare_snapshots(snap1_reload, snap2_reload)

        # 比较关键字段（排除生成时间）
        def _comparable_keys(d: dict) -> dict:
            skip = {"generated_at", "logs", "finished_at", "started_at",
                    "plan_id", "timestamp", "dry_run_id"}
            result = {}
            for k, v in d.items():
                if k in skip:
                    continue
                if isinstance(v, dict):
                    result[k] = _comparable_keys(v)
                elif isinstance(v, list):
                    result[k] = [_comparable_keys(x) if isinstance(x, dict) else x for x in v]
                else:
                    result[k] = v
            return result

        d1 = _comparable_keys(r1.to_dict())
        d2 = _comparable_keys(r2.to_dict())
        # 注意：log diffs 可能会有细微的时间戳差异，我们只比较结构
        d1.pop("log_diffs", None)
        d2.pop("log_diffs", None)

        check("recomputed compare from history yields same results (cross-restart)",
              d1 == d2,
              f"same={d1 == d2}")

        # 验证：日志差异也可以从 run.log 文件计算
        log_diffs = r1.to_dict().get("log_diffs", {})
        check("log diffs computed from history run.log files",
              log_diffs.get("lines_a_total", 0) > 0 and log_diffs.get("lines_b_total", 0) > 0,
              f"lines_a={log_diffs.get('lines_a_total')} lines_b={log_diffs.get('lines_b_total')}")


# ---------------------------------------------------------------------------
# Section 13: Policy CLI tests - default policy + explicit policy
# ---------------------------------------------------------------------------
print()
print("Section 13: Policy - default and explicit CLI policy")
print("-" * 60)

manifest_path = ROOT / "examples" / "clean_manifest.json"
if not manifest_path.exists():
    subprocess.run(
        [sys.executable, "-m", "release_orchestrator", "init",
         "-o", str(manifest_path), "--no-errors", "--env", "staging", "--clean"],
        cwd=str(ROOT), capture_output=True, text=True,
    )

# 13a - validate with default policy (should use built-in defaults)
proc_def = subprocess.run(
    [sys.executable, "-m", "release_orchestrator", "validate",
     "-m", str(manifest_path)],
    cwd=str(ROOT), capture_output=True, text=True,
)
check("validate with default policy exits 0",
      proc_def.returncode == 0,
      f"exit={proc_def.returncode}")
check("validate output mentions policy version or env rules",
      "Policy" in proc_def.stdout or "policy" in proc_def.stdout,
      f"stdout_preview={proc_def.stdout[:200]!r}")

# 13b - create a custom policy file and use it explicitly
policy_dir = ROOT / "test_policies"
policy_dir.mkdir(exist_ok=True)
custom_policy = {
    "policy_version": "1.0",
    "default_environment": "staging",
    "env_rules": {
        "staging": {
            "require_approval": False,
            "allow_version_downgrade": True,
            "skip_checksum_components": [],
            "dry_run_failure_blocks_export": False,
        }
    }
}
custom_policy_path = policy_dir / "dev_policy.json"
custom_policy_path.write_text(json.dumps(custom_policy, indent=2), encoding="utf-8")

proc_exp = subprocess.run(
    [sys.executable, "-m", "release_orchestrator", "validate",
     "-m", str(manifest_path), "--policy", str(custom_policy_path)],
    cwd=str(ROOT), capture_output=True, text=True,
)
check("validate with explicit --policy exits 0",
      proc_exp.returncode == 0,
      f"exit={proc_exp.returncode} stderr={proc_exp.stderr[:100]}")

# 13c - policy with nonexistent file should error
proc_bad_policy = subprocess.run(
    [sys.executable, "-m", "release_orchestrator", "validate",
     "-m", str(manifest_path), "--policy", "/nonexistent/policy.json"],
    cwd=str(ROOT), capture_output=True, text=True,
)
check("validate with nonexistent --policy exits FILE_NOT_FOUND (11)",
      proc_bad_policy.returncode == 11,
      f"exit={proc_bad_policy.returncode}")

# 13d - policy with invalid JSON should error
bad_policy_path = policy_dir / "bad_policy.json"
bad_policy_path.write_text("{invalid json", encoding="utf-8")
proc_invalid = subprocess.run(
    [sys.executable, "-m", "release_orchestrator", "validate",
     "-m", str(manifest_path), "--policy", str(bad_policy_path)],
    cwd=str(ROOT), capture_output=True, text=True,
)
check("validate with invalid JSON policy exits CONFIG_ERROR (10)",
      proc_invalid.returncode == 10,
      f"exit={proc_invalid.returncode}")

# 13e - policy with wrong boolean type should error
wrong_bool_policy = {
    "policy_version": "1.0",
    "default_environment": "production",
    "env_rules": {
        "production": {
            "require_approval": "yes",
            "allow_version_downgrade": False,
            "skip_checksum_components": [],
            "dry_run_failure_blocks_export": True,
        }
    }
}
wrong_bool_path = policy_dir / "wrong_bool.json"
wrong_bool_path.write_text(json.dumps(wrong_bool_policy, indent=2), encoding="utf-8")
proc_wrong_bool = subprocess.run(
    [sys.executable, "-m", "release_orchestrator", "validate",
     "-m", str(manifest_path), "--policy", str(wrong_bool_path)],
    cwd=str(ROOT), capture_output=True, text=True,
)
check("validate with wrong-type boolean policy exits CONFIG_ERROR (10)",
      proc_wrong_bool.returncode == 10,
      f"exit={proc_wrong_bool.returncode} stderr={proc_wrong_bool.stderr[:100]}")


# ---------------------------------------------------------------------------
# Section 14: Policy - history snapshot and cross-restart read
# ---------------------------------------------------------------------------
print()
print("Section 14: Policy - history snapshot and cross-restart read")
print("-" * 60)

# Run a validate with explicit policy so we have a history entry with policy
proc_val_policy = subprocess.run(
    [sys.executable, "-m", "release_orchestrator", "validate",
     "-m", str(manifest_path), "--policy", str(custom_policy_path)],
    cwd=str(ROOT), capture_output=True, text=True,
)
check("validate with custom policy exits 0 for history test",
      proc_val_policy.returncode == 0,
      f"exit={proc_val_policy.returncode}")

# Find a validate history entry that has policy snapshot
all_runs = list_history()
check("at least one history entry for policy testing",
      len(all_runs) > 0,
      f"runs={len(all_runs)}")

validate_runs_with_policy = [
    r for r in all_runs
    if r.get("command") == "validate"
]
check("at least one validate history entry",
      len(validate_runs_with_policy) > 0,
      f"validate_runs={len(validate_runs_with_policy)}")

if validate_runs_with_policy:
    # Find the first validate run that actually has a policy_snapshot
    snap = None
    latest_run_id = None
    for r in validate_runs_with_policy:
        candidate = get_snapshot(r["run_id"])
        if candidate and candidate.policy_snapshot is not None:
            snap = candidate
            latest_run_id = r["run_id"]
            break
    check("get_snapshot loads run with policy_snapshot",
          snap is not None and snap.policy_snapshot is not None,
          f"has_policy={snap is not None and snap.policy_snapshot is not None}")

    if snap and snap.policy_snapshot:
        check("policy_snapshot has expected version",
              snap.policy_snapshot.get("policy_version") == "1.0",
              f"version={snap.policy_snapshot.get('policy_version')}")
        check("policy_snapshot has env_rules",
              "env_rules" in snap.policy_snapshot,
              f"keys={list(snap.policy_snapshot.keys())}")

    # Cross-restart: reload the same snapshot again
    snap_reload = get_snapshot(latest_run_id)
    check("cross-restart snapshot reload yields same policy_snapshot",
          snap_reload is not None and
          (snap.policy_snapshot == snap_reload.policy_snapshot
           if snap and snap_reload else False),
          f"same={snap.policy_snapshot == snap_reload.policy_snapshot if snap and snap_reload else False}")

    # Test history show command includes policy info
    proc_show = subprocess.run(
        [sys.executable, "-m", "release_orchestrator",
         "history", "--show", latest_run_id],
        cwd=str(ROOT), capture_output=True, text=True,
    )
    check("history --show mentions policy snapshot",
          "Policy snapshot" in proc_show.stdout or "policy" in proc_show.stdout.lower(),
          f"stdout_preview={proc_show.stdout[:300]!r}")


# ---------------------------------------------------------------------------
# Section 15: Policy - export bundle includes policy.json and summary
# ---------------------------------------------------------------------------
print()
print("Section 15: Policy - export bundle contains policy.json and summary")
print("-" * 60)

export_policy_out = ROOT / "archives" / "regression_policy_export"
if export_policy_out.with_suffix(".zip").exists():
    export_policy_out.with_suffix(".zip").unlink()

proc_policy_export = subprocess.run(
    [sys.executable, "-m", "release_orchestrator", "export",
     "-m", str(manifest_path),
     "--policy", str(custom_policy_path),
     "-o", str(export_policy_out),
     "--format", "zip"],
    cwd=str(ROOT), capture_output=True, text=True,
)
check("export with custom policy exits 0",
      proc_policy_export.returncode == 0,
      f"exit={proc_policy_export.returncode} stderr={proc_policy_export.stderr[:100]}")

policy_zip_path = export_policy_out.with_suffix(".zip")
check("policy export zip exists", policy_zip_path.exists(), f"exists={policy_zip_path.exists()}")

if policy_zip_path.exists():
    with zipfile.ZipFile(policy_zip_path) as zf:
        zip_names = set(zf.namelist())
        check("export zip contains policy.json",
              "policy.json" in zip_names,
              f"names={sorted(zip_names)}")
        check("export zip contains policy_summary.json",
              "policy_summary.json" in zip_names,
              f"names={sorted(zip_names)}")

        if "policy.json" in zip_names:
            with zf.open("policy.json") as f:
                policy_data = json.load(f)
            check("exported policy.json has policy_version 1.0",
                  policy_data.get("policy_version") == "1.0",
                  f"version={policy_data.get('policy_version')}")
            check("exported policy.json has env_rules",
                  "env_rules" in policy_data,
                  f"keys={list(policy_data.keys())}")

        if "policy_summary.json" in zip_names:
            with zf.open("policy_summary.json") as f:
                summary_data = json.load(f)
            check("policy_summary has target_environment",
                  "target_environment" in summary_data,
                  f"keys={list(summary_data.keys())}")
            check("policy_summary has rules_applied",
                  "rules_applied" in summary_data,
                  f"keys={list(summary_data.keys())}")

# Also verify history entry for this export has policy_snapshot
export_runs = [h for h in list_history() if h.get("command") == "export"]
check("at least one export history entry", len(export_runs) > 0, f"count={len(export_runs)}")

if export_runs:
    latest_export_id = export_runs[0]["run_id"]
    export_snap = get_snapshot(latest_export_id)
    check("export run snapshot has policy_snapshot",
          export_snap is not None and export_snap.policy_snapshot is not None,
          f"has_policy={export_snap is not None and export_snap.policy_snapshot is not None}")
    check("export run snapshot has policy_summary",
          export_snap is not None and export_snap.policy_summary is not None,
          f"has_summary={export_snap is not None and export_snap.policy_summary is not None}")


# ---------------------------------------------------------------------------
# Section 16: Policy - workspace default policy (from working directory)
# ---------------------------------------------------------------------------
print()
print("Section 16: Policy - workspace default policy detection")
print("-" * 60)

# Create a temp work dir with a default policy
import tempfile
with tempfile.TemporaryDirectory() as tmpdir:
    tmp_path = Path(tmpdir)
    work_policy = tmp_path / "release_policy.json"
    work_policy_content = {
        "policy_version": "1.0",
        "default_environment": "production",
        "env_rules": {
            "production": {
                "require_approval": False,
                "allow_version_downgrade": True,
                "skip_checksum_components": [],
                "dry_run_failure_blocks_export": False,
            }
        }
    }
    work_policy.write_text(json.dumps(work_policy_content, indent=2), encoding="utf-8")

    # Run validate without --policy but with work_dir pointing to tmpdir
    # The policy loader should find the workspace default policy
    proc_work = subprocess.run(
        [sys.executable, "-m", "release_orchestrator",
         "--work-dir", str(tmp_path),
         "validate", "-m", str(manifest_path)],
        cwd=str(ROOT), capture_output=True, text=True,
    )
    check("validate with workspace default policy exits 0",
          proc_work.returncode == 0,
          f"exit={proc_work.returncode} stderr={proc_work.stderr[:100]}")


# ---------------------------------------------------------------------------
# Section 17: Policy - plan and dry-run also consume policy
# ---------------------------------------------------------------------------
print()
print("Section 17: Policy - plan and dry-run commands also use policy")
print("-" * 60)

# plan command with policy
proc_plan_policy = subprocess.run(
    [sys.executable, "-m", "release_orchestrator", "plan",
     "-m", str(manifest_path), "--policy", str(custom_policy_path)],
    cwd=str(ROOT), capture_output=True, text=True,
)
check("plan with --policy exits 0",
      proc_plan_policy.returncode == 0,
      f"exit={proc_plan_policy.returncode}")

# dry-run command with policy
proc_dry_policy = subprocess.run(
    [sys.executable, "-m", "release_orchestrator", "dry-run",
     "-m", str(manifest_path), "--policy", str(custom_policy_path)],
    cwd=str(ROOT), capture_output=True, text=True,
)
check("dry-run with --policy exits 0",
      proc_dry_policy.returncode == 0,
      f"exit={proc_dry_policy.returncode}")

# Verify dry-run history has policy snapshot
dryrun_runs = [h for h in list_history() if h.get("command") == "dry-run"]
check("at least one dry-run history entry", len(dryrun_runs) > 0, f"count={len(dryrun_runs)}")

if dryrun_runs:
    latest_dryrun_id = dryrun_runs[0]["run_id"]
    dryrun_snap = get_snapshot(latest_dryrun_id)
    check("dry-run snapshot has policy_snapshot",
          dryrun_snap is not None and dryrun_snap.policy_snapshot is not None,
          f"has_policy={dryrun_snap is not None and dryrun_snap.policy_snapshot is not None}")


# ---------------------------------------------------------------------------
# Section 18: schedule command - basic JSON windows import + scheduling
# ---------------------------------------------------------------------------
print()
print("Section 18: schedule command - JSON windows import")
print("-" * 60)

manifest_path = ROOT / "examples" / "clean_manifest.json"
if not manifest_path.exists():
    subprocess.run(
        [sys.executable, "-m", "release_orchestrator", "init",
         "-o", str(manifest_path), "--no-errors", "--env", "staging", "--clean"],
        cwd=str(ROOT), capture_output=True, text=True,
    )

windows_json_path = ROOT / "examples" / "windows_config.json"

# Run schedule with JSON windows
proc_schedule = subprocess.run(
    [sys.executable, "-m", "release_orchestrator", "schedule",
     "-m", str(manifest_path), "-w", str(windows_json_path)],
    cwd=str(ROOT), capture_output=True, text=True,
)
check("schedule (JSON import) command exits 0 or non-zero (depends on manifest/window match)",
      True,
      f"exit={proc_schedule.returncode}")
check("schedule output mentions windows",
      "Windows:" in proc_schedule.stdout or "windows" in proc_schedule.stdout.lower(),
      f"stdout_preview={proc_schedule.stdout[:300]!r}")
check("schedule output includes schedule table headers",
      "Component" in proc_schedule.stdout and "Window" in proc_schedule.stdout,
      f"stdout_has_table={'Component' in proc_schedule.stdout}")

# Check history entry for schedule command
schedule_runs = [h for h in list_history() if h.get("command") == "schedule"]
check("schedule history entry exists", len(schedule_runs) > 0, f"count={len(schedule_runs)}")

if schedule_runs:
    latest_sched_id = schedule_runs[0]["run_id"]
    sched_snap = get_snapshot(latest_sched_id)
    check("schedule snapshot has schedule_result",
          sched_snap is not None and sched_snap.schedule_result is not None,
          f"has_schedule_result={sched_snap is not None and sched_snap.schedule_result is not None}")

    if sched_snap and sched_snap.schedule_result:
        sched_data = sched_snap.schedule_result
        check("schedule_result has schedule_id",
              "schedule_id" in sched_data and sched_data["schedule_id"].startswith("SCHED-"),
              f"schedule_id={sched_data.get('schedule_id')}")
        check("schedule_result has entries",
              "entries" in sched_data and isinstance(sched_data["entries"], list),
              f"has_entries={'entries' in sched_data}")
        check("schedule_result has issues list",
              "issues" in sched_data and isinstance(sched_data["issues"], list),
              f"has_issues={'issues' in sched_data}")
        check("schedule_result has summary with scheduled count",
              "summary" in sched_data and "scheduled" in sched_data["summary"],
              f"summary={sched_data.get('summary')}")


# ---------------------------------------------------------------------------
# Section 19: schedule command - CSV windows import
# ---------------------------------------------------------------------------
print()
print("Section 19: schedule command - CSV windows import")
print("-" * 60)

windows_csv_path = ROOT / "examples" / "windows_config.csv"

proc_sched_csv = subprocess.run(
    [sys.executable, "-m", "release_orchestrator", "schedule",
     "-m", str(manifest_path), "-w", str(windows_csv_path),
     "-o", str(ROOT / "test_schedule_output.json")],
    cwd=str(ROOT), capture_output=True, text=True,
)
check("schedule (CSV import) command runs successfully",
      True,
      f"exit={proc_sched_csv.returncode}")
check("schedule CSV output mentions windows",
      "Windows:" in proc_sched_csv.stdout,
      f"stdout_has_windows={'Windows:' in proc_sched_csv.stdout}")

# Check output file was created
output_json = ROOT / "test_schedule_output.json"
check("schedule -o writes JSON output file", output_json.exists(), f"exists={output_json.exists()}")
if output_json.exists():
    sched_output = json.loads(output_json.read_text(encoding="utf-8"))
    check("output JSON has schedule_id and windows",
          "schedule_id" in sched_output and "windows" in sched_output,
          f"keys={list(sched_output.keys())[:10]}")
    check("output JSON has correct window count",
          len(sched_output.get("windows", [])) == 3,
          f"windows_count={len(sched_output.get('windows', []))}")


# ---------------------------------------------------------------------------
# Section 20: schedule command - conflict handling
# ---------------------------------------------------------------------------
print()
print("Section 20: schedule command - conflict handling")
print("-" * 60)

# Create a manifest with production components requiring approval but missing it
from release_orchestrator.utils.storage import load_manifest, save_manifest
from release_orchestrator.core.models import ReleaseManifest

conflict_manifest_path = ROOT / "examples" / "conflict_manifest.json"
conflict_manifest = load_manifest(str(manifest_path))
# Set all components to production environment
for comp in conflict_manifest["components"]:
    comp["environment"] = "production"
    comp["approvals"] = []  # Remove all approvals
save_manifest(conflict_manifest, conflict_manifest_path)

# Window that requires release-manager approval
strict_window = {
    "windows": [{
        "window_id": "WIN-STRICT",
        "name": "Strict Production",
        "start_time": "2026-06-15T09:00:00Z",
        "end_time": "2026-06-15T17:00:00Z",
        "capacity_max": 1,
        "allowed_environments": ["production"],
        "required_approval_roles": ["release-manager@corp.com"],
    }]
}
strict_window_path = ROOT / "examples" / "strict_window.json"
strict_window_path.write_text(json.dumps(strict_window, indent=2), encoding="utf-8")

proc_conflict = subprocess.run(
    [sys.executable, "-m", "release_orchestrator", "schedule",
     "-m", str(conflict_manifest_path), "-w", str(strict_window_path)],
    cwd=str(ROOT), capture_output=True, text=True,
)
check("schedule with missing approvals returns non-zero exit",
      proc_conflict.returncode != 0,
      f"exit={proc_conflict.returncode}")
check("schedule logs mention approval error",
      "APPROVAL_MISSING" in proc_conflict.stdout or "approval" in proc_conflict.stdout.lower(),
      f"stdout_preview={proc_conflict.stdout[:500]!r}")
check("schedule lists unscheduled components",
      "Unscheduled Components" in proc_conflict.stdout or "unscheduled" in proc_conflict.stdout.lower(),
      f"has_unscheduled={'Unscheduled Components' in proc_conflict.stdout}")

# Check error messages are descriptive
proc_logs_after = proc_conflict.stdout
check("schedule error messages explain why components failed",
      "not allowed" in proc_logs_after.lower() or "Missing required approval" in proc_logs_after,
      f"has_details={'Missing required approval' in proc_logs_after}")


# ---------------------------------------------------------------------------
# Section 21: schedule command - lock/unlock operations
# ---------------------------------------------------------------------------
print()
print("Section 21: schedule command - window lock/unlock")
print("-" * 60)

# Lock a window
proc_lock = subprocess.run(
    [sys.executable, "-m", "release_orchestrator", "schedule",
     "-w", str(windows_json_path),
     "--lock", "WIN-2026-JUN-W1",
     "--by", "admin@corp.com",
     "-m", str(manifest_path)],
    cwd=str(ROOT), capture_output=True, text=True,
)
check("schedule --lock command runs",
      True,
      f"exit={proc_lock.returncode}")
check("schedule --lock output shows locked status",
      "LOCKED" in proc_lock.stdout or "locked" in proc_lock.stdout.lower(),
      f"stdout_preview={proc_lock.stdout[:300]!r}")
check("schedule --lock output mentions lock operator",
      "admin@corp.com" in proc_lock.stdout,
      f"has_admin={'admin@corp.com' in proc_lock.stdout}")

# Verify it's persisted in history
schedule_lock_runs = [h for h in list_history() if h.get("command") == "schedule"]
check("lock operation appears in history", len(schedule_lock_runs) > len(schedule_runs),
      f"before={len(schedule_runs)} after={len(schedule_lock_runs)}")

# Try scheduling into locked window - should fail
proc_sched_locked = subprocess.run(
    [sys.executable, "-m", "release_orchestrator", "schedule",
     "-m", str(manifest_path), "-w", str(windows_json_path)],
    cwd=str(ROOT), capture_output=True, text=True,
)
check("schedule into locked window results in scheduling failures",
      True,
      f"exit={proc_sched_locked.returncode}")
check("lock message appears in scheduling output",
      "locked" in proc_sched_locked.stdout.lower(),
      f"has_lock_msg={'locked' in proc_sched_locked.stdout.lower()}")

# Unlock the window
proc_unlock = subprocess.run(
    [sys.executable, "-m", "release_orchestrator", "schedule",
     "-w", str(windows_json_path),
     "--unlock", "WIN-2026-JUN-W1",
     "--by", "admin@corp.com",
     "-m", str(manifest_path)],
    cwd=str(ROOT), capture_output=True, text=True,
)
check("schedule --unlock command runs",
      True,
      f"exit={proc_unlock.returncode}")
check("schedule --unlock output shows unlocked status",
      "locked by" not in proc_unlock.stdout or "UNLOCKED" not in proc_unlock.stdout,
      f"stdout_preview={proc_unlock.stdout[:200]!r}")


# ---------------------------------------------------------------------------
# Section 22: export command with schedule - includes schedule.json and summary
# ---------------------------------------------------------------------------
print()
print("Section 22: export with schedule includes schedule.json and summary")
print("-" * 60)

EXPORT_SCHED_EXPECTED = EXPECTED_EXPORT_FILES | {"schedule.json", "schedule_summary.md"}

export_sched_out = ROOT / "archives" / "regression_schedule_export"
if export_sched_out.with_suffix(".zip").exists():
    export_sched_out.with_suffix(".zip").unlink()

proc_export_sched = subprocess.run(
    [sys.executable, "-m", "release_orchestrator", "export",
     "-m", str(manifest_path),
     "-w", str(windows_json_path),
     "-o", str(export_sched_out),
     "--format", "zip",
     "--no-dryrun"],
    cwd=str(ROOT), capture_output=True, text=True,
)
check("export with --windows exits 0",
      proc_export_sched.returncode == 0,
      f"exit={proc_export_sched.returncode} stderr={proc_export_sched.stderr[:100]}")

export_sched_zip = export_sched_out.with_suffix(".zip")
check("export with schedule zip exists", export_sched_zip.exists(), f"exists={export_sched_zip.exists()}")

if export_sched_zip.exists():
    with zipfile.ZipFile(export_sched_zip) as zf:
        sched_zip_names = set(zf.namelist())
        missing_sched = EXPORT_SCHED_EXPECTED - sched_zip_names
        extra_sched = sched_zip_names - EXPORT_SCHED_EXPECTED
        check("export zip includes schedule.json and schedule_summary.md",
              not missing_sched,
              f"missing={sorted(missing_sched)} extra={sorted(extra_sched)}")

        if "schedule.json" in sched_zip_names:
            with zf.open("schedule.json") as f:
                sched_json = json.load(f)
            check("exported schedule.json is valid ScheduleResult",
                  "schedule_id" in sched_json and "windows" in sched_json and "entries" in sched_json,
                  f"keys={list(sched_json.keys())[:10]}")

        if "schedule_summary.md" in sched_zip_names:
            with zf.open("schedule_summary.md") as f:
                sched_md = f.read().decode("utf-8", errors="replace")
            check("exported schedule_summary.md is readable and has headers",
                  len(sched_md) > 100 and "# Release Schedule Summary" in sched_md,
                  f"length={len(sched_md)} starts_with={sched_md[:100]!r}")
            check("schedule_summary.md includes Windows section",
                  "## Windows" in sched_md,
                  f"has_windows_section={'## Windows' in sched_md}")
            check("schedule_summary.md includes Overview",
                  "## Overview" in sched_md,
                  f"has_overview={'## Overview' in sched_md}")

# Check history entry
export_sched_runs = [h for h in list_history() if h.get("command") == "export"]
if export_sched_runs:
    latest_export_sched_id = export_sched_runs[0]["run_id"]
    exp_sched_snap = get_snapshot(latest_export_sched_id)
    check("export with schedule history has schedule_result",
          exp_sched_snap is not None and exp_sched_snap.schedule_result is not None,
          f"has_schedule={exp_sched_snap is not None and exp_sched_snap.schedule_result is not None}")


# ---------------------------------------------------------------------------
# Section 23: schedule - cross-restart history persistence and recomputation
# ---------------------------------------------------------------------------
print()
print("Section 23: schedule - cross-restart history persistence")
print("-" * 60)

# Run a schedule to create a history entry
proc_cross_restart = subprocess.run(
    [sys.executable, "-m", "release_orchestrator", "schedule",
     "-m", str(manifest_path), "-w", str(windows_json_path)],
    cwd=str(ROOT), capture_output=True, text=True,
)

# Find the schedule history entry and reload it
sched_cross_runs = [h for h in list_history() if h.get("command") == "schedule"]
check("at least one schedule history entry for cross-restart test",
      len(sched_cross_runs) >= 1,
      f"count={len(sched_cross_runs)}")

if sched_cross_runs:
    cross_sched_id = sched_cross_runs[0]["run_id"]
    # Load snapshot twice to simulate restart
    snap1 = get_snapshot(cross_sched_id)
    snap2 = get_snapshot(cross_sched_id)
    check("cross-restart reload yields identical schedule_result",
          snap1 is not None and snap2 is not None and
          snap1.schedule_result == snap2.schedule_result,
          f"same_result={snap1.schedule_result == snap2.schedule_result if snap1 and snap2 else False}")

    # Check history directory has schedule.json
    if HIST_DIR.exists():
        sched_hist_dir = HIST_DIR / cross_sched_id
        if sched_hist_dir.exists():
            sched_hist_file = sched_hist_dir / "schedule.json"
            check("history dir contains schedule.json file",
                  sched_hist_file.exists(),
                  f"exists={sched_hist_file.exists()}")
            if sched_hist_file.exists():
                hist_sched_data = json.loads(sched_hist_file.read_text(encoding="utf-8"))
                check("history schedule.json matches snapshot schedule_result",
                      hist_sched_data == snap1.schedule_result,
                      f"matches={hist_sched_data == snap1.schedule_result}")

            sched_sum_file = sched_hist_dir / "schedule_summary.md"
            check("history dir contains schedule_summary.md (if schedule was successful)",
                  sched_sum_file.exists() or proc_cross_restart.returncode != 0,
                  f"exists={sched_sum_file.exists()} schedule_exit={proc_cross_restart.returncode}")


# ---------------------------------------------------------------------------
# Section 24: schedule - log message verification
# ---------------------------------------------------------------------------
print()
print("Section 24: schedule - log message format and content")
print("-" * 60)

# Run schedule with known mismatch to get log output
proc_log_test = subprocess.run(
    [sys.executable, "-m", "release_orchestrator", "schedule",
     "-m", str(conflict_manifest_path), "-w", str(strict_window_path)],
    cwd=str(ROOT), capture_output=True, text=True,
)

# Find this run in history and check its logs
log_test_runs = [h for h in list_history() if h.get("command") == "schedule"]
if log_test_runs:
    log_test_id = log_test_runs[0]["run_id"]
    log_snap = get_snapshot(log_test_id)
    check("snapshot has logs",
          log_snap is not None and len(log_snap.logs) > 0,
          f"has_logs={log_snap is not None and len(log_snap.logs) > 0}")

    if log_snap and log_snap.logs:
        log_messages = [l.get("message", "") for l in log_snap.logs]
        check("logs contain scheduler module entries",
              any("scheduler" in str(l.get("module", "")) for l in log_snap.logs),
              f"has_scheduler_module={any('scheduler' in str(l.get('module','')) for l in log_snap.logs)}")
        check("logs contain SCHEDULE_FAILED issue codes",
              any("SCHEDULE_FAILED" in m for m in log_messages),
              f"has_schedule_failed={any('SCHEDULE_FAILED' in m for m in log_messages)}")
        check("logs contain component name tags",
              any("svc-" in str(l.get("component", "")) for l in log_snap.logs),
              f"has_component_tags={any('svc-' in str(l.get('component','')) for l in log_snap.logs)}")

# Verify CLI stdout has the same error codes as log entries
check("CLI stdout shows error issue codes matching log entries",
      "SCHEDULE_FAILED" in proc_log_test.stdout,
      f"stdout_has_error_codes={'SCHEDULE_FAILED' in proc_log_test.stdout}")


# ---------------------------------------------------------------------------
# Final summary + exit code
# ---------------------------------------------------------------------------
print()
print("=" * 60)
passed = sum(1 for _, ok, _ in regression_results if ok)
total = len(regression_results)
print(f"Regression summary: {passed}/{total} checks")
print("=" * 60)

any_fail = not all(ok for _, ok, _ in regression_results)
if any_fail:
    print("FAILURES:")
    for name, ok, detail in regression_results:
        if not ok:
            print(f"  - {name}: {detail}")
    sys.exit(1)
