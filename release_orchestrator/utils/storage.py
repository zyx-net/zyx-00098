"""Persistence utilities for configuration, snapshots, and history.

Handles reading/writing manifest files, saving per-run execution
snapshots, listing historical runs, and exporting archives.
"""
from __future__ import annotations

import json
import os
import shutil
import tarfile
import tempfile
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from ..core.models import (
    ExecutionSnapshot,
    ReleaseManifest,
    ReleasePlan,
    ValidationResult,
    now_iso,
)
from ..utils.exit_codes import (
    EXIT_CONFIG_ERROR,
    EXIT_FILE_NOT_FOUND,
    EXIT_HISTORY_ERROR,
)
from .logger import get_logger


LOG = get_logger()
MODULE = "storage"

DEFAULT_WORK_DIR = ".release_orchestrator"
HISTORY_DIR = "history"
SNAPSHOT_FILE = "snapshot.json"
LOG_FILE = "run.log"
MANIFEST_SNAPSHOT_FILE = "manifest_snapshot.json"
POLICY_SNAPSHOT_FILE = "policy_snapshot.json"
POLICY_SUMMARY_FILE = "policy_summary.json"
VALIDATION_FILE = "validation.json"
PLAN_FILE = "release_plan.json"
ROLLBACK_PLAN_FILE = "rollback_plan.json"
DRY_RUN_FILE = "dry_run_result.json"
SCHEDULE_FILE = "schedule.json"
SCHEDULE_SUMMARY_FILE = "schedule_summary.md"
CONFIG_FILE = "config.json"
SCHEMES_DIR = "schemes"
SCHEME_HISTORY_FILE = "scheme_operations.log"


def get_work_dir(base: Optional[str] = None) -> Path:
    """Return the base working directory for all orchestrator state."""
    root = Path(base) if base else Path.cwd()
    return root / DEFAULT_WORK_DIR


def ensure_work_dir(base: Optional[str] = None) -> Path:
    """Create work directory structure if not already present."""
    work = get_work_dir(base)
    (work / HISTORY_DIR).mkdir(parents=True, exist_ok=True)
    (work / SCHEMES_DIR).mkdir(parents=True, exist_ok=True)
    return work


def get_schemes_dir(base: Optional[str] = None) -> Path:
    """Return the directory that stores named scheduling schemes."""
    work = get_work_dir(base)
    schemes_dir = work / SCHEMES_DIR
    schemes_dir.mkdir(parents=True, exist_ok=True)
    return schemes_dir


def _scheme_file_path(scheme_name: str, base: Optional[str] = None) -> Path:
    """Return the file path for a specific named scheme."""
    safe_name = _sanitize_scheme_name(scheme_name)
    return get_schemes_dir(base) / f"{safe_name}.json"


def _sanitize_scheme_name(name: str) -> str:
    """Sanitize scheme name to be safe for use as a filename."""
    import re
    cleaned = re.sub(r"[^\w\-.]", "_", name.strip())
    if not cleaned:
        raise ValueError("Scheme name cannot be empty after sanitization")
    return cleaned


def scheme_exists(scheme_name: str, base: Optional[str] = None) -> bool:
    """Check if a scheme with the given name exists."""
    return _scheme_file_path(scheme_name, base).exists()


def save_scheme(scheme, base: Optional[str] = None, overwrite: bool = False) -> Path:
    """Save a ReleaseScheme to disk.

    Args:
        scheme: The ReleaseScheme instance to save.
        base: Optional base working directory.
        overwrite: If True, overwrite existing scheme with same name.
                   If False (default), raise FileExistsError.

    Returns:
        Path to the saved scheme file.

    Raises:
        FileExistsError: If scheme exists and overwrite is False.
        IOError: If writing to disk fails.
    """
    path = _scheme_file_path(scheme.scheme_name, base)
    if path.exists() and not overwrite:
        raise FileExistsError(
            f"Scheme '{scheme.scheme_name}' already exists. Use overwrite=True to replace it."
        )

    if overwrite and path.exists():
        scheme.updated_at = now_iso()

    try:
        save_json(scheme.to_dict(), path)
    except Exception as exc:
        raise IOError(f"Failed to save scheme '{scheme.scheme_name}': {exc}") from exc

    LOG.info(
        MODULE,
        f"Saved scheme",
        scheme_name=scheme.scheme_name,
        path=str(path),
        overwrite=overwrite,
    )
    _log_scheme_operation("save", scheme.scheme_name, base=base, extra={"overwrite": overwrite})
    return path


def load_scheme(scheme_name: str, base: Optional[str] = None):
    """Load a ReleaseScheme from disk by name.

    Args:
        scheme_name: Name of the scheme to load.
        base: Optional base working directory.

    Returns:
        The loaded ReleaseScheme instance.

    Raises:
        FileNotFoundError: If the scheme does not exist.
        ValueError: If the scheme JSON is invalid or missing required fields.
    """
    from ..core.models import ReleaseScheme

    path = _scheme_file_path(scheme_name, base)
    if not path.exists():
        raise FileNotFoundError(f"Scheme not found: '{scheme_name}'")

    try:
        data = load_json(path)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid scheme JSON for '{scheme_name}': {exc}") from exc
    except Exception as exc:
        raise IOError(f"Failed to read scheme '{scheme_name}': {exc}") from exc

    try:
        scheme = ReleaseScheme.from_dict(data)
    except (KeyError, TypeError) as exc:
        raise ValueError(f"Scheme '{scheme_name}' has missing or invalid fields: {exc}") from exc

    LOG.info(
        MODULE,
        f"Loaded scheme",
        scheme_name=scheme_name,
        path=str(path),
    )
    return scheme


def list_schemes(base: Optional[str] = None) -> List[Dict[str, Any]]:
    """List all saved schemes with their metadata.

    Returns:
        List of dicts with keys: name, created_at, updated_at, description, tags, path.
    """
    schemes_dir = get_schemes_dir(base)
    results = []
    if not schemes_dir.exists():
        return results

    for entry in sorted(schemes_dir.iterdir()):
        if entry.is_file() and entry.suffix == ".json":
            try:
                data = load_json(entry)
                results.append({
                    "name": data.get("scheme_name", entry.stem),
                    "created_at": data.get("created_at"),
                    "updated_at": data.get("updated_at"),
                    "created_by": data.get("created_by", "unknown"),
                    "description": data.get("description"),
                    "tags": data.get("tags", []),
                    "windows_count": len(data.get("release_windows", [])),
                    "waves_count": len(data.get("waves", [])),
                    "path": str(entry),
                })
            except Exception:
                continue
    return results


def delete_scheme(scheme_name: str, base: Optional[str] = None) -> bool:
    """Delete a scheme by name.

    Returns:
        True if the scheme was deleted, False if it didn't exist.
    """
    path = _scheme_file_path(scheme_name, base)
    if not path.exists():
        return False
    try:
        path.unlink()
        LOG.info(MODULE, f"Deleted scheme", scheme_name=scheme_name, path=str(path))
        _log_scheme_operation("delete", scheme_name, base=base)
        return True
    except Exception as exc:
        raise IOError(f"Failed to delete scheme '{scheme_name}': {exc}") from exc


def export_scheme_to_file(scheme_name: str, output_path: str, base: Optional[str] = None) -> Path:
    """Export an existing scheme to a JSON file at the given path.

    This is different from save_scheme in that it writes to an arbitrary
    user-specified path rather than the internal schemes directory.
    """
    scheme = load_scheme(scheme_name, base)
    out = Path(output_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    try:
        save_json(scheme.to_dict(), out)
    except Exception as exc:
        raise IOError(f"Failed to export scheme '{scheme_name}': {exc}") from exc

    LOG.info(
        MODULE,
        f"Exported scheme",
        scheme_name=scheme_name,
        output_path=str(out),
    )
    _log_scheme_operation(
        "export", scheme_name, base=base, extra={"output_path": str(out)}
    )
    return out


def import_scheme_from_file(
    input_path: str,
    scheme_name: Optional[str] = None,
    base: Optional[str] = None,
    overwrite: bool = False,
):
    """Import a scheme from an external JSON file into the schemes store.

    Args:
        input_path: Path to the scheme JSON file.
        scheme_name: Optional name override; if None, uses the name in the file.
        base: Optional base working directory.
        overwrite: Whether to overwrite an existing scheme with the same name.

    Returns:
        The imported ReleaseScheme instance.
    """
    from ..core.models import ReleaseScheme

    p = Path(input_path)
    if not p.exists():
        raise FileNotFoundError(f"Scheme file not found: {input_path}")
    try:
        data = load_json(p)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid JSON in scheme file '{input_path}': {exc}") from exc

    if scheme_name:
        data["scheme_name"] = scheme_name
        if "updated_at" in data:
            data["updated_at"] = None

    try:
        scheme = ReleaseScheme.from_dict(data)
    except (KeyError, TypeError) as exc:
        raise ValueError(f"Scheme file '{input_path}' has missing or invalid fields: {exc}") from exc

    save_scheme(scheme, base=base, overwrite=overwrite)
    _log_scheme_operation(
        "import", scheme.scheme_name, base=base, extra={"source_path": str(p)}
    )
    return scheme


def clone_scheme(
    source_name: str,
    target_name: str,
    base: Optional[str] = None,
    overwrite: bool = False,
    created_by: str = "admin@corp.com",
):
    """Clone an existing scheme into a new scheme.

    Args:
        source_name: Name of the scheme to clone.
        target_name: Name of the new scheme.
        base: Optional base working directory.
        overwrite: If True, overwrite existing scheme with same name.
        created_by: User creating the cloned scheme.

    Returns:
        The cloned ReleaseScheme instance.

    Raises:
        FileNotFoundError: If the source scheme does not exist.
        ValueError: If the target name is invalid.
        FileExistsError: If target scheme exists and overwrite is False.
        IOError: If reading from or writing to disk fails.
    """
    from ..core.models import ReleaseScheme

    _sanitize_scheme_name(target_name)

    if not scheme_exists(source_name, base):
        raise FileNotFoundError(f"Source scheme not found: '{source_name}'")

    target_path = _scheme_file_path(target_name, base)
    if target_path.exists() and not overwrite:
        raise FileExistsError(
            f"Scheme '{target_name}' already exists. Use overwrite=True to replace it."
        )

    source_scheme = load_scheme(source_name, base)

    source_dict = source_scheme.to_dict()
    source_dict["scheme_name"] = target_name
    source_dict["created_at"] = now_iso()
    source_dict["created_by"] = created_by
    source_dict["updated_at"] = None

    metadata = dict(source_dict.get("metadata", {}) or {})
    metadata["cloned_from"] = source_name
    metadata["source"] = "scheme_clone"
    source_dict["metadata"] = metadata

    cloned_scheme = ReleaseScheme.from_dict(source_dict)

    save_scheme(cloned_scheme, base=base, overwrite=overwrite)
    _log_scheme_operation(
        "clone",
        target_name,
        base=base,
        extra={"source": source_name, "overwrite": overwrite},
    )
    return cloned_scheme


def _log_scheme_operation(
    action: str,
    scheme_name: str,
    base: Optional[str] = None,
    extra: Optional[Dict[str, Any]] = None,
) -> None:
    """Append a scheme operation to the scheme operations log."""
    import getpass
    work = get_work_dir(base)
    log_path = work / SCHEME_HISTORY_FILE
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        entry = {
            "timestamp": now_iso(),
            "action": action,
            "scheme_name": scheme_name,
            "user": getpass.getuser() or "unknown",
            "extra": extra or {},
        }
        with log_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception:
        pass


def get_run_dir(run_id: str, base: Optional[str] = None) -> Path:
    """Return the directory that stores a specific run's artifacts."""
    work = get_work_dir(base)
    return work / HISTORY_DIR / run_id


def ensure_run_dir(run_id: str, base: Optional[str] = None) -> Path:
    run_dir = get_run_dir(run_id, base)
    run_dir.mkdir(parents=True, exist_ok=True)
    return run_dir


def load_manifest(path: str) -> ReleaseManifest:
    """Load a ReleaseManifest from a JSON file."""
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"Manifest file not found: {path}")
    try:
        with p.open("r", encoding="utf-8") as f:
            data = json.load(f)
        LOG.info(MODULE, f"Loaded manifest from {path}", components=len(data.get("components", [])))
        return ReleaseManifest.from_dict(data)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid manifest JSON: {exc}")


def save_manifest(manifest: ReleaseManifest, path: str) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("w", encoding="utf-8") as f:
        f.write(manifest.to_json())
    LOG.info(MODULE, f"Saved manifest to {path}")


def save_json(obj: Any, path: Path) -> None:
    with path.open("w", encoding="utf-8") as f:
        json.dump(obj, f, indent=2, ensure_ascii=False, default=str)


def load_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def persist_run_artifacts(
    run_id: str,
    command: str,
    started_at: str,
    exit_code: int,
    config_snapshot: Optional[Dict[str, Any]] = None,
    manifest_snapshot: Optional[Dict[str, Any]] = None,
    policy_snapshot: Optional[Dict[str, Any]] = None,
    policy_summary: Optional[Dict[str, Any]] = None,
    validation_result: Optional[Dict[str, Any]] = None,
    release_plan: Optional[Dict[str, Any]] = None,
    rollback_plan: Optional[Dict[str, Any]] = None,
    dry_run_result: Optional[Dict[str, Any]] = None,
    schedule_result: Optional[Dict[str, Any]] = None,
    schedule_summary: Optional[str] = None,
    logs_text: str = "",
    logs_entries: Optional[List[Dict[str, Any]]] = None,
    base: Optional[str] = None,
) -> Path:
    """Persist all artifacts of a single command invocation to disk."""
    run_dir = ensure_run_dir(run_id, base)
    finished_at = now_iso()

    if manifest_snapshot is not None:
        save_json(manifest_snapshot, run_dir / MANIFEST_SNAPSHOT_FILE)
    if policy_snapshot is not None:
        save_json(policy_snapshot, run_dir / POLICY_SNAPSHOT_FILE)
    if policy_summary is not None:
        save_json(policy_summary, run_dir / POLICY_SUMMARY_FILE)
    if validation_result is not None:
        save_json(validation_result, run_dir / VALIDATION_FILE)
    if release_plan is not None:
        save_json(release_plan, run_dir / PLAN_FILE)
    if rollback_plan is not None:
        save_json(rollback_plan, run_dir / ROLLBACK_PLAN_FILE)
    if dry_run_result is not None:
        save_json(dry_run_result, run_dir / DRY_RUN_FILE)
    if schedule_result is not None:
        save_json(schedule_result, run_dir / SCHEDULE_FILE)
    if schedule_summary is not None:
        (run_dir / SCHEDULE_SUMMARY_FILE).write_text(schedule_summary, encoding="utf-8")
    if config_snapshot is not None:
        save_json(config_snapshot, run_dir / CONFIG_FILE)

    with (run_dir / LOG_FILE).open("w", encoding="utf-8") as f:
        f.write(logs_text)

    snapshot = ExecutionSnapshot(
        run_id=run_id,
        command=command,
        started_at=started_at,
        finished_at=finished_at,
        exit_code=exit_code,
        config_snapshot=config_snapshot,
        manifest_snapshot=manifest_snapshot,
        policy_snapshot=policy_snapshot,
        policy_summary=policy_summary,
        validation_result=validation_result,
        release_plan=release_plan,
        rollback_plan=rollback_plan,
        dry_run_result=dry_run_result,
        schedule_result=schedule_result,
        logs=logs_entries or [],
    )
    save_json(snapshot.to_dict(), run_dir / SNAPSHOT_FILE)

    LOG.info(MODULE, f"Persisted run artifacts", run_id=run_id, run_dir=str(run_dir))
    return run_dir


def list_history(base: Optional[str] = None) -> List[Dict[str, Any]]:
    """List all stored execution snapshots, newest first."""
    work = get_work_dir(base)
    history_dir = work / HISTORY_DIR
    if not history_dir.exists():
        return []
    results = []
    for entry in sorted(history_dir.iterdir(), reverse=True):
        if entry.is_dir():
            snap_file = entry / SNAPSHOT_FILE
            if snap_file.exists():
                try:
                    snap = load_json(snap_file)
                    results.append({
                        "run_id": snap.get("run_id"),
                        "command": snap.get("command"),
                        "started_at": snap.get("started_at"),
                        "finished_at": snap.get("finished_at"),
                        "exit_code": snap.get("exit_code"),
                        "run_dir": str(entry),
                    })
                except Exception:
                    continue
    return results


def get_snapshot(run_id: str, base: Optional[str] = None) -> Optional[ExecutionSnapshot]:
    """Load a single execution snapshot by run_id."""
    run_dir = get_run_dir(run_id, base)
    snap_file = run_dir / SNAPSHOT_FILE
    if not snap_file.exists():
        return None
    try:
        return ExecutionSnapshot.from_dict(load_json(snap_file))
    except Exception as exc:
        LOG.error(MODULE, f"Failed to load snapshot {run_id}: {exc}")
        return None


def export_archive(
    run_id: str,
    output_path: str,
    base: Optional[str] = None,
    fmt: str = "zip",
) -> Path:
    """Export a run's directory into an archive (zip or tar.gz)."""
    run_dir = get_run_dir(run_id, base)
    if not run_dir.exists():
        raise FileNotFoundError(f"Run directory not found: {run_dir}")

    out = Path(output_path)
    out.parent.mkdir(parents=True, exist_ok=True)

    fmt_lower = fmt.lower()
    if fmt_lower == "tar.gz":
        archive_path = out if str(out).endswith(".tar.gz") else out.with_suffix(".tar.gz")
        with tarfile.open(archive_path, "w:gz") as tf:
            tf.add(run_dir, arcname=run_id)
    elif fmt_lower == "tar":
        archive_path = out if str(out).endswith(".tar") else out.with_suffix(".tar")
        with tarfile.open(archive_path, "w:") as tf:
            tf.add(run_dir, arcname=run_id)
    else:
        archive_path = out if str(out).endswith(".zip") else out.with_suffix(".zip")
        with zipfile.ZipFile(archive_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for file_path in run_dir.rglob("*"):
                if file_path.is_file():
                    arcname = f"{run_id}/{file_path.relative_to(run_dir)}"
                    zf.write(file_path, arcname=arcname)

    LOG.info(MODULE, f"Exported archive", output=str(archive_path), format=fmt_lower)
    return archive_path


def export_full_manifest_archive(
    manifest: ReleaseManifest,
    output_path: str,
    extra_files: Optional[Dict[str, Any]] = None,
    fmt: str = "zip",
) -> Path:
    """Export a manifest plus all generated artifacts into a single archive.

    This is used by the `export` command to bundle the manifest,
    validation result, release plan, rollback plan, dry-run output,
    and logs into one portable package.
    """
    out = Path(output_path)
    out.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)

        save_json(manifest.to_dict(), tmp_path / "manifest.json")

        extra_files = extra_files or {}
        for name, content in extra_files.items():
            if isinstance(content, (dict, list)):
                save_json(content, tmp_path / name)
            elif isinstance(content, str):
                (tmp_path / name).write_text(content, encoding="utf-8")
            else:
                save_json({"content": str(content)}, tmp_path / name)

        fmt_lower = fmt.lower()
        if fmt_lower == "tar.gz":
            archive_path = out if str(out).endswith(".tar.gz") else out.with_suffix(".tar.gz")
            with tarfile.open(archive_path, "w:gz") as tf:
                for f in tmp_path.iterdir():
                    tf.add(f, arcname=f.name)
        else:
            archive_path = out if str(out).endswith(".zip") else out.with_suffix(".zip")
            with zipfile.ZipFile(archive_path, "w", zipfile.ZIP_DEFLATED) as zf:
                for f in tmp_path.rglob("*"):
                    if f.is_file():
                        zf.write(f, arcname=f.relative_to(tmp_path))

    LOG.info(MODULE, f"Exported manifest archive", output=str(archive_path))
    return archive_path
