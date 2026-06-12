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
    LockPermissionConfig,
    ReleaseLock,
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
LOCKS_DIR = "locks"
LOCKS_INDEX_FILE = "locks_index.json"
LOCK_HISTORY_FILE = "lock_operations.log"
LOCK_PERMISSION_FILE = "lock_permissions.json"
DEFAULT_LOCK_PERMISSIONS = LockPermissionConfig()


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


# ---------------------------------------------------------------------------
# Release Lock persistence
# ---------------------------------------------------------------------------

VALID_ENVIRONMENTS = {"dev", "test", "staging", "production"}


def _locks_dir(base: Optional[str] = None) -> Path:
    work = get_work_dir(base)
    d = work / LOCKS_DIR
    d.mkdir(parents=True, exist_ok=True)
    return d


def _locks_index_path(base: Optional[str] = None) -> Path:
    return _locks_dir(base).parent / LOCKS_INDEX_FILE


def _lock_file_path(lock_id: str, base: Optional[str] = None) -> Path:
    return _locks_dir(base) / f"{lock_id}.json"


def _generate_lock_id() -> str:
    """Generate a short, sortable lock id."""
    ts = datetime.utcnow().strftime("%Y%m%d%H%M%S")
    import secrets
    return f"LOCK-{ts}-{secrets.token_hex(4).upper()}"


def _validate_lock(lock: ReleaseLock) -> List[str]:
    """Validate a lock, return a list of error strings."""
    errors: List[str] = []
    from ..core.models import LockScope

    if lock.environment and lock.environment.lower() not in VALID_ENVIRONMENTS:
        errors.append(
            f"Invalid environment '{lock.environment}'. "
            f"Must be one of: {sorted(VALID_ENVIRONMENTS)}"
        )

    if lock.scope == LockScope.ENVIRONMENT:
        if not lock.environment:
            errors.append("ENVIRONMENT scope requires 'environment' field")

    if lock.scope == LockScope.SERVICE:
        if not lock.service_name or not lock.service_name.strip():
            errors.append("SERVICE scope requires non-empty 'service_name'")

    if lock.scope == LockScope.WINDOW:
        if not (lock.window_start and lock.window_end):
            errors.append("WINDOW scope requires 'window_start' and 'window_end'")
        else:
            try:
                s = datetime.fromisoformat(lock.window_start.replace("Z", "+00:00"))
                e = datetime.fromisoformat(lock.window_end.replace("Z", "+00:00"))
                if s >= e:
                    errors.append("WINDOW lock: window_start must be earlier than window_end")
            except Exception as exc:
                errors.append(f"WINDOW lock: invalid ISO datetime: {exc}")

    if lock.expires_at:
        try:
            exp = datetime.fromisoformat(lock.expires_at.replace("Z", "+00:00"))
        except Exception as exc:
            errors.append(f"Invalid expires_at ISO datetime: {exc}")

    return errors


def _read_lock_index(base: Optional[str] = None) -> Dict[str, str]:
    path = _locks_index_path(base)
    if not path.exists():
        return {}
    try:
        data = load_json(path)
        if isinstance(data, dict):
            return {str(k): str(v) for k, v in data.items()}
    except Exception:
        pass
    return {}


def _write_lock_index(index: Dict[str, str], base: Optional[str] = None) -> None:
    save_json(index, _locks_index_path(base))


def list_locks(base: Optional[str] = None, include_expired: bool = False) -> List[ReleaseLock]:
    """List all saved locks.

    Args:
        base: Optional base work directory.
        include_expired: If True, also return locks that have already expired.

    Returns:
        List of ReleaseLock instances sorted by created_at (newest first).
    """
    index = _read_lock_index(base)
    locks: List[ReleaseLock] = []
    for lock_id in index:
        path = _lock_file_path(lock_id, base)
        if not path.exists():
            continue
        try:
            lock = ReleaseLock.from_dict(load_json(path))
            if not include_expired and lock.is_expired():
                continue
            locks.append(lock)
        except Exception:
            continue
    locks.sort(key=lambda l: l.created_at, reverse=True)
    return locks


def get_lock(lock_id: str, base: Optional[str] = None) -> Optional[ReleaseLock]:
    """Load a single lock by id, returning None if not found or expired."""
    path = _lock_file_path(lock_id, base)
    if not path.exists():
        return None
    try:
        lock = ReleaseLock.from_dict(load_json(path))
        return lock
    except Exception:
        return None


def save_lock(
    lock: ReleaseLock,
    base: Optional[str] = None,
    overwrite: bool = False,
) -> ReleaseLock:
    """Persist a lock to disk.

    Args:
        lock: The lock to save.
        base: Optional base work directory.
        overwrite: If True, allow overwriting an existing lock with the same id.

    Returns:
        The saved lock (with generated lock_id if not provided).

    Raises:
        ValueError: If lock validation fails.
        FileExistsError: If lock_id exists and overwrite=False.
        FileExistsError: If a lock with identical scope/parameters already exists.
        IOError: If writing to disk fails.
    """
    from ..core.models import LockScope

    errors = _validate_lock(lock)
    if errors:
        raise ValueError("Lock validation failed: " + "; ".join(errors))

    if not lock.lock_id:
        lock.lock_id = _generate_lock_id()

    existing = list_locks(base, include_expired=True)
    for e in existing:
        if e.lock_id == lock.lock_id:
            if not overwrite:
                raise FileExistsError(
                    f"Lock with id '{lock.lock_id}' already exists. "
                    f"Use overwrite=True to replace it."
                )
            continue
        if e.is_expired():
            continue
        if e.scope != lock.scope:
            continue
        duplicate = False
        if lock.scope == LockScope.GLOBAL:
            duplicate = True
        elif lock.scope == LockScope.ENVIRONMENT:
            duplicate = (e.environment or "").lower() == (lock.environment or "").lower()
        elif lock.scope == LockScope.SERVICE:
            same_svc = (e.service_name or "").lower() == (lock.service_name or "").lower()
            same_env = (e.environment or "").lower() == (lock.environment or "").lower()
            both_env_none = e.environment is None and lock.environment is None
            duplicate = same_svc and (same_env or both_env_none)
        elif lock.scope == LockScope.WINDOW:
            if e.window_start and e.window_end and lock.window_start and lock.window_end:
                duplicate = e.overlaps_window(lock.window_start, lock.window_end)
        if duplicate:
            raise FileExistsError(
                f"Duplicate lock scope: {lock.description_short()} "
                f"collides with existing lock {e.lock_id}."
            )

    path = _lock_file_path(lock.lock_id, base)
    try:
        save_json(lock.to_dict(), path)
        index = _read_lock_index(base)
        index[lock.lock_id] = now_iso()
        _write_lock_index(index, base)
    except IOError as exc:
        raise IOError(f"Failed to write lock file: {exc}")

    _log_lock_operation(
        "save" if overwrite and path.exists() else "create",
        lock.lock_id,
        base=base,
        extra={"scope": lock.scope.value, "overwrite": overwrite},
    )
    return lock


def delete_lock(lock_id: str, base: Optional[str] = None) -> bool:
    """Delete a lock by id. Returns True if it existed and was removed.

    Raises:
        FileNotFoundError: If no such lock exists.
        IOError: If removing the file fails.
    """
    path = _lock_file_path(lock_id, base)
    if not path.exists():
        raise FileNotFoundError(f"Lock not found: '{lock_id}'")

    lock = get_lock(lock_id, base)
    scope_val = lock.scope.value if lock else "unknown"

    try:
        path.unlink()
        index = _read_lock_index(base)
        if lock_id in index:
            del index[lock_id]
            _write_lock_index(index, base)
    except OSError as exc:
        raise IOError(f"Failed to delete lock '{lock_id}': {exc}")

    _log_lock_operation(
        "delete",
        lock_id,
        base=base,
        extra={"scope": scope_val},
    )
    return True


def check_locks_for_operation(
    base: Optional[str] = None,
    environment: Optional[str] = None,
    service_names: Optional[List[str]] = None,
    window_start: Optional[str] = None,
    window_end: Optional[str] = None,
) -> List[ReleaseLock]:
    """Return the list of active locks that block an operation.

    Args:
        base: Optional base work directory.
        environment: Target environment of the operation.
        service_names: List of component/service names involved.
        window_start: ISO datetime start of scheduling window.
        window_end: ISO datetime end of scheduling window.

    Returns:
        List of ReleaseLock instances that would block the operation.
    """
    active_locks = list_locks(base, include_expired=False)
    if not active_locks:
        return []

    blockers: List[ReleaseLock] = []
    for lock in active_locks:
        blocks = False
        if lock.scope.value == "global":
            blocks = True
        elif lock.scope.value == "environment" and environment:
            blocks = lock.covers_environment(environment)
        elif lock.scope.value == "service" and service_names:
            for svc in service_names:
                if lock.covers_service(svc, environment):
                    blocks = True
                    break
        elif lock.scope.value == "window":
            if window_start and window_end:
                blocks = lock.overlaps_window(window_start, window_end)
                if not blocks and environment:
                    blocks = lock.covers_environment(environment)
            elif environment:
                blocks = lock.covers_environment(environment)
        if blocks:
            blockers.append(lock)
    return blockers


def export_all_locks(
    output_path: str,
    base: Optional[str] = None,
    include_expired: bool = False,
) -> Path:
    """Export all locks into a single JSON file.

    Returns:
        The Path the file was written to.
    """
    locks = list_locks(base, include_expired=include_expired)
    data = {
        "exported_at": now_iso(),
        "count": len(locks),
        "include_expired": include_expired,
        "locks": [l.to_dict() for l in locks],
    }
    out = Path(output_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    save_json(data, out)
    return out


def import_locks_from_file(
    input_path: str,
    base: Optional[str] = None,
    overwrite: bool = False,
    created_by: Optional[str] = None,
) -> Tuple[int, List[str]]:
    """Import locks from a JSON export file.

    Args:
        input_path: Path to the locks JSON export file.
        base: Optional base work directory.
        overwrite: If True, overwrite locks with the same id.
        created_by: If provided, override created_by for all imported locks.

    Returns:
        Tuple of (imported_count, [list of error strings]).

    Raises:
        FileNotFoundError: If input_path doesn't exist.
        ValueError: If the file has an invalid structure.
        IOError: If reading fails.
    """
    path = Path(input_path)
    if not path.exists():
        raise FileNotFoundError(f"Locks import file not found: {input_path}")
    try:
        data = load_json(path)
    except Exception as exc:
        raise ValueError(f"Invalid locks JSON: {exc}")

    if not isinstance(data, dict) or "locks" not in data:
        raise ValueError(
            "Invalid locks export file: missing 'locks' array at top level"
        )

    imported = 0
    errors: List[str] = []
    raw_list = data.get("locks", [])
    if not isinstance(raw_list, list):
        raise ValueError("'locks' must be an array")

    for i, raw in enumerate(raw_list):
        try:
            lock = ReleaseLock.from_dict(raw)
            if created_by:
                lock.created_by = created_by
            save_lock(lock, base=base, overwrite=overwrite)
            imported += 1
        except ValueError as exc:
            errors.append(f"Lock #{i} ({raw.get('lock_id', '?')}) invalid: {exc}")
        except FileExistsError as exc:
            errors.append(f"Lock #{i} ({raw.get('lock_id', '?')}) skipped: {exc}")
        except Exception as exc:
            errors.append(f"Lock #{i} ({raw.get('lock_id', '?')}) error: {exc}")
    return imported, errors


def _log_lock_operation(
    action: str,
    lock_id: str,
    base: Optional[str] = None,
    extra: Optional[Dict[str, Any]] = None,
) -> None:
    """Append a lock operation to the lock operations log."""
    import getpass
    work = get_work_dir(base)
    log_path = work / LOCK_HISTORY_FILE
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        entry = {
            "timestamp": now_iso(),
            "action": action,
            "lock_id": lock_id,
            "user": getpass.getuser() or "unknown",
            "extra": extra or {},
        }
        with log_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception:
        pass


def load_lock_permissions(base: Optional[str] = None) -> LockPermissionConfig:
    """Load lock permission config from disk, falling back to defaults.

    Resolution order:
      1. <work_dir>/lock_permissions.json
      2. <cwd>/lock_permissions.json
      3. Built-in default
    """
    candidates: List[Path] = []
    if base:
        candidates.append(get_work_dir(base) / LOCK_PERMISSION_FILE)
    candidates.append(Path.cwd() / LOCK_PERMISSION_FILE)

    for c in candidates:
        if c.exists():
            try:
                return LockPermissionConfig.from_dict(load_json(c))
            except Exception:
                continue
    return DEFAULT_LOCK_PERMISSIONS


def save_lock_permissions(
    config: LockPermissionConfig,
    base: Optional[str] = None,
) -> Path:
    """Write a lock permission config to the work directory."""
    work = get_work_dir(base)
    path = work / LOCK_PERMISSION_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    save_json(config.to_dict(), path)
    return path
