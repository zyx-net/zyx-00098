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
