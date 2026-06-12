"""`lock` command family - manage release deployment locks."""
from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any, List, Optional

from .base import CommandResult
from ..core.models import LockScope, ReleaseLock
from ..utils.exit_codes import (
    EXIT_CONFIG_ERROR,
    EXIT_FILE_NOT_FOUND,
    EXIT_LOCK_ALREADY_EXISTS,
    EXIT_LOCK_BLOCKED_OPERATION,
    EXIT_LOCK_IO_ERROR,
    EXIT_LOCK_NOT_FOUND,
    EXIT_LOCK_PERMISSION_DENIED,
    EXIT_LOCK_VALIDATION_FAILED,
    EXIT_OK,
)
from ..utils.logger import get_logger
from ..utils.storage import (
    check_locks_for_operation,
    delete_lock,
    export_all_locks,
    get_lock,
    import_locks_from_file,
    list_locks,
    load_lock_permissions,
    save_lock,
    VALID_ENVIRONMENTS,
)

LOG = get_logger()
MODULE = "cmd.lock"


def add_parser(subparsers: "argparse._SubParsersAction") -> None:
    p = subparsers.add_parser(
        "lock",
        help="Manage release deployment locks (freeze windows)",
        description="Create, list, remove, import, export release locks that block plan/schedule/rollback operations.",
    )
    sub = p.add_subparsers(dest="lock_action", metavar="<action>")

    p_add = sub.add_parser("add", help="Create a new release lock")
    p_add.add_argument(
        "--scope", choices=["global", "environment", "service", "window"],
        required=True, help="Scope of the lock",
    )
    p_add.add_argument(
        "-e", "--environment", default=None,
        help="Environment name (required for 'environment' scope, optional for 'service')",
    )
    p_add.add_argument(
        "-s", "--service", default=None,
        help="Service/component name (required for 'service' scope)",
    )
    p_add.add_argument(
        "--window-start", default=None, metavar="ISO_DATETIME",
        help="Start of the time window (ISO 8601, required for 'window' scope)",
    )
    p_add.add_argument(
        "--window-end", default=None, metavar="ISO_DATETIME",
        help="End of the time window (ISO 8601, required for 'window' scope)",
    )
    p_add.add_argument(
        "--window-id", default=None,
        help="Optional reference window id (for 'window' scope)",
    )
    p_add.add_argument(
        "-r", "--reason", default="",
        help="Human-readable reason for the lock",
    )
    p_add.add_argument(
        "--by", default="admin@corp.com",
        help="User creating the lock",
    )
    p_add.add_argument(
        "--expires", default=None, metavar="ISO_DATETIME",
        help="When the lock automatically expires (ISO 8601)",
    )
    p_add.add_argument(
        "--force", "--overwrite", dest="overwrite", action="store_true",
        help="Overwrite an existing lock with identical scope",
    )
    p_add.add_argument(
        "--as-role", default="SRE_ADMIN",
        help="Role to assume for permission checking (default: SRE_ADMIN)",
    )
    p_add.add_argument(
        "--id", default=None, dest="lock_id",
        help="Explicit lock id (auto-generated if omitted)",
    )

    p_list = sub.add_parser("list", help="List active release locks")
    p_list.add_argument(
        "--all", "--include-expired", dest="include_expired", action="store_true",
        help="Also show expired locks",
    )
    p_list.add_argument(
        "--json", action="store_true",
        help="Print as JSON instead of a table",
    )

    p_show = sub.add_parser("show", help="Show details of a specific lock")
    p_show.add_argument("lock_id", help="Id of the lock to inspect")

    p_remove = sub.add_parser("remove", help="Remove (unlock) a release lock")
    p_remove.add_argument("lock_id", help="Id of the lock to remove")
    p_remove.add_argument(
        "--by", default="admin@corp.com",
        help="User performing the removal (used for permission checks)",
    )
    p_remove.add_argument(
        "--as-role", default="SRE_OPS",
        help="Role to assume for permission checking (default: SRE_OPS)",
    )
    p_remove.add_argument(
        "-f", "--force", dest="force", action="store_true",
        help="Force removal (skip permission check - SRE_ADMIN only check via role)",
    )

    p_export = sub.add_parser("export", help="Export all locks to a JSON file")
    p_export.add_argument(
        "-o", "--output", required=True,
        help="Path to write the locks JSON file",
    )
    p_export.add_argument(
        "--all", "--include-expired", dest="include_expired", action="store_true",
        help="Also export expired locks",
    )

    p_import = sub.add_parser("import", help="Import locks from a JSON export file")
    p_import.add_argument("file", help="Path to the locks JSON file")
    p_import.add_argument(
        "--force", "--overwrite", dest="overwrite", action="store_true",
        help="Overwrite locks with same id (default: skip)",
    )
    p_import.add_argument(
        "--by", default=None,
        help="Override created_by for all imported locks",
    )

    p_check = sub.add_parser(
        "check",
        help="Check whether an operation would be blocked by active locks",
    )
    p_check.add_argument("-e", "--environment", default=None, help="Target environment")
    p_check.add_argument(
        "-s", "--services", default=None,
        help="Comma-separated list of service/component names",
    )
    p_check.add_argument("--window-start", default=None, help="Window start ISO datetime")
    p_check.add_argument("--window-end", default=None, help="Window end ISO datetime")

    p.set_defaults(func=_run)


def _run(args: argparse.Namespace, **_: Any) -> CommandResult:
    action = getattr(args, "lock_action", None)
    if not action:
        print("ERROR: No lock action specified. Use one of: add, list, show, remove, import, export, check")
        return CommandResult(exit_code=EXIT_CONFIG_ERROR.code, run_id="")

    base = getattr(args, "work_dir", None)

    handlers = {
        "add": _handle_add,
        "list": _handle_list,
        "show": _handle_show,
        "remove": _handle_remove,
        "export": _handle_export,
        "import": _handle_import,
        "check": _handle_check,
    }
    handler = handlers.get(action)
    if not handler:
        print(f"ERROR: Unknown lock action: '{action}'. Use one of: {', '.join(handlers.keys())}")
        return CommandResult(exit_code=EXIT_CONFIG_ERROR.code, run_id="")
    return handler(args, base)


def _scope_from_arg(value: str) -> LockScope:
    mapping = {
        "global": LockScope.GLOBAL,
        "environment": LockScope.ENVIRONMENT,
        "service": LockScope.SERVICE,
        "window": LockScope.WINDOW,
    }
    return mapping[value]


def _handle_add(args: argparse.Namespace, base: Optional[str]) -> CommandResult:
    perms = load_lock_permissions(base)
    role = getattr(args, "as_role", "SRE_ADMIN")
    if not perms.can_create(role):
        msg = (
            f"Permission denied: role '{role}' is not allowed to create locks. "
            f"Allowed roles for lock creation: "
            + ", ".join(r for r, p in perms.roles.items() if "create_locks" in p)
        )
        print(f"ERROR: {msg}")
        LOG.error(MODULE, f"Lock create permission denied: role={role}")
        _log_permission_denied("create", role=role, base=base)
        return CommandResult(exit_code=EXIT_LOCK_PERMISSION_DENIED.code, run_id="")

    scope = _scope_from_arg(args.scope)
    environment = getattr(args, "environment", None)
    service_name = getattr(args, "service", None)
    window_start = getattr(args, "window_start", None)
    window_end = getattr(args, "window_end", None)
    window_id = getattr(args, "window_id", None)
    reason = getattr(args, "reason", "")
    created_by = getattr(args, "by", "admin@corp.com")
    expires_at = getattr(args, "expires", None)
    overwrite = getattr(args, "overwrite", False)
    lock_id = getattr(args, "lock_id", None)

    lock = ReleaseLock(
        lock_id=lock_id or "",
        scope=scope,
        environment=environment,
        service_name=service_name,
        window_id=window_id,
        window_start=window_start,
        window_end=window_end,
        reason=reason,
        created_by=created_by,
        expires_at=expires_at,
    )

    try:
        saved = save_lock(lock, base=base, overwrite=overwrite)
    except ValueError as exc:
        print(f"ERROR: {exc}")
        LOG.error(MODULE, f"Lock validation failed: {exc}")
        return CommandResult(exit_code=EXIT_LOCK_VALIDATION_FAILED.code, run_id="")
    except FileExistsError as exc:
        print(f"ERROR: {exc}")
        LOG.error(MODULE, f"Lock already exists: {exc}")
        return CommandResult(exit_code=EXIT_LOCK_ALREADY_EXISTS.code, run_id="")
    except IOError as exc:
        print(f"ERROR: {exc}")
        LOG.error(MODULE, f"Lock IO error: {exc}")
        return CommandResult(exit_code=EXIT_LOCK_IO_ERROR.code, run_id="")

    print(f"\nLock created successfully: {saved.lock_id}")
    _print_lock_detail(saved)
    return CommandResult(
        exit_code=EXIT_OK.code,
        run_id="",
        extra_artifacts={"lock": saved.to_dict()},
    )


def _handle_list(args: argparse.Namespace, base: Optional[str]) -> CommandResult:
    include_expired = getattr(args, "include_expired", False)
    as_json = getattr(args, "json", False)
    locks = list_locks(base, include_expired=include_expired)

    if as_json:
        from ..utils.storage import now_iso
        data = {
            "generated_at": now_iso(),
            "count": len(locks),
            "include_expired": include_expired,
            "locks": [l.to_dict() for l in locks],
        }
        import json as _json
        print(_json.dumps(data, indent=2, ensure_ascii=False))
        return CommandResult(exit_code=EXIT_OK.code, run_id="", extra_artifacts=data)

    if not locks:
        label = "(including expired)" if include_expired else "(active only)"
        print(f"No locks found {label}.")
        return CommandResult(exit_code=EXIT_OK.code, run_id="")

    print(f"\n=== Release Locks ({len(locks)} total) ===\n")
    header = f"{'ID':<28} {'Scope':<13} {'Target':<30} {'By':<20} {'Created':<25} {'Expires':<25} Reason"
    print(header)
    print("-" * len(header))
    for l in locks:
        target = l.description_short().split("=", 1)[-1] if "=" in l.description_short() else l.description_short()
        expires = l.expires_at or "(never)"
        expired_tag = " [EXPIRED]" if l.is_expired() else ""
        print(
            f"{l.lock_id:<28} {l.scope.value:<13} {target[:30]:<30} "
            f"{l.created_by[:20]:<20} {l.created_at[:24]:<25} "
            f"{expires[:24]:<25} {l.reason[:60]}{expired_tag}"
        )
    print()
    return CommandResult(exit_code=EXIT_OK.code, run_id="")


def _handle_show(args: argparse.Namespace, base: Optional[str]) -> CommandResult:
    lock_id = args.lock_id
    lock = get_lock(lock_id, base=base)
    if not lock:
        print(f"ERROR: Lock not found: '{lock_id}'")
        return CommandResult(exit_code=EXIT_LOCK_NOT_FOUND.code, run_id="")

    print(f"\n=== Lock Details: {lock.lock_id} ===")
    _print_lock_detail(lock)
    return CommandResult(exit_code=EXIT_OK.code, run_id="", extra_artifacts={"lock": lock.to_dict()})


def _print_lock_detail(lock: ReleaseLock) -> None:
    print(f"  Scope        : {lock.scope.value}")
    if lock.environment:
        print(f"  Environment  : {lock.environment}")
    if lock.service_name:
        print(f"  Service      : {lock.service_name}")
    if lock.window_id:
        print(f"  Window ID    : {lock.window_id}")
    if lock.window_start:
        print(f"  Window Start : {lock.window_start}")
    if lock.window_end:
        print(f"  Window End   : {lock.window_end}")
    print(f"  Created By   : {lock.created_by}")
    print(f"  Created At   : {lock.created_at}")
    print(f"  Expires At   : {lock.expires_at or '(never)'}")
    if lock.is_expired():
        print(f"  Status       : EXPIRED")
    else:
        print(f"  Status       : ACTIVE")
    if lock.reason:
        print(f"  Reason       : {lock.reason}")
    if lock.metadata:
        print(f"  Metadata     : {lock.metadata}")
    print(f"  Description  : {lock.description_short()}")


def _handle_remove(args: argparse.Namespace, base: Optional[str]) -> CommandResult:
    lock_id = args.lock_id
    lock = get_lock(lock_id, base=base)
    if not lock:
        print(f"ERROR: Lock not found: '{lock_id}'")
        return CommandResult(exit_code=EXIT_LOCK_NOT_FOUND.code, run_id="")

    actor = getattr(args, "by", "admin@corp.com")
    role = getattr(args, "as_role", "SRE_OPS")
    force = getattr(args, "force", False)

    perms = load_lock_permissions(base)

    if not perms.can_remove(role, lock.created_by, actor):
        msg = (
            f"Permission denied: role '{role}' (user '{actor}') cannot remove lock "
            f"{lock_id} created by '{lock.created_by}'. "
            f"Required: SRE_ADMIN role (remove_any_lock) or owner with remove_own_lock permission."
        )
        print(f"ERROR: {msg}")
        LOG.error(MODULE, f"Lock remove permission denied: role={role} actor={actor} lock_owner={lock.created_by}")
        _log_permission_denied(
            "remove",
            role=role,
            actor=actor,
            lock_id=lock_id,
            lock_owner=lock.created_by,
            base=base,
        )
        return CommandResult(exit_code=EXIT_LOCK_PERMISSION_DENIED.code, run_id="")

    try:
        delete_lock(lock_id, base=base)
    except IOError as exc:
        print(f"ERROR: {exc}")
        LOG.error(MODULE, f"Lock delete IO error: {exc}")
        return CommandResult(exit_code=EXIT_LOCK_IO_ERROR.code, run_id="")

    print(f"\nLock removed successfully: {lock_id}")
    print(f"  Scope   : {lock.scope.value}")
    print(f"  Removed by: {actor} (role={role})")
    return CommandResult(exit_code=EXIT_OK.code, run_id="")


def _handle_export(args: argparse.Namespace, base: Optional[str]) -> CommandResult:
    output_path = args.output
    include_expired = getattr(args, "include_expired", False)
    try:
        path = export_all_locks(output_path, base=base, include_expired=include_expired)
    except IOError as exc:
        print(f"ERROR: Failed to export locks: {exc}")
        LOG.error(MODULE, f"Lock export IO error: {exc}")
        return CommandResult(exit_code=EXIT_LOCK_IO_ERROR.code, run_id="")

    locks = list_locks(base, include_expired=include_expired)
    print(f"\nExported {len(locks)} lock(s) to: {path}")
    return CommandResult(exit_code=EXIT_OK.code, run_id="", extra_artifacts={"export_path": str(path)})


def _handle_import(args: argparse.Namespace, base: Optional[str]) -> CommandResult:
    file_path = args.file
    overwrite = getattr(args, "overwrite", False)
    created_by = getattr(args, "by", None)

    try:
        count, errors = import_locks_from_file(
            file_path,
            base=base,
            overwrite=overwrite,
            created_by=created_by,
        )
    except FileNotFoundError as exc:
        print(f"ERROR: {exc}")
        return CommandResult(exit_code=EXIT_FILE_NOT_FOUND.code, run_id="")
    except ValueError as exc:
        print(f"ERROR: {exc}")
        LOG.error(MODULE, f"Lock import validation: {exc}")
        return CommandResult(exit_code=EXIT_LOCK_VALIDATION_FAILED.code, run_id="")
    except IOError as exc:
        print(f"ERROR: Failed to import locks: {exc}")
        LOG.error(MODULE, f"Lock import IO error: {exc}")
        return CommandResult(exit_code=EXIT_LOCK_IO_ERROR.code, run_id="")

    print(f"\nLocks import complete: {count} imported successfully")
    if errors:
        print(f"  Errors / Skipped ({len(errors)}):")
        for e in errors:
            print(f"    - {e}")
    return CommandResult(
        exit_code=EXIT_OK.code if not errors else EXIT_LOCK_VALIDATION_FAILED.code,
        run_id="",
        extra_artifacts={"imported": count, "errors": errors},
    )


def _handle_check(args: argparse.Namespace, base: Optional[str]) -> CommandResult:
    environment = getattr(args, "environment", None)
    services_raw = getattr(args, "services", None)
    services: Optional[List[str]] = None
    if services_raw:
        services = [s.strip() for s in services_raw.split(",") if s.strip()]
    window_start = getattr(args, "window_start", None)
    window_end = getattr(args, "window_end", None)

    blockers = check_locks_for_operation(
        base=base,
        environment=environment,
        service_names=services,
        window_start=window_start,
        window_end=window_end,
    )

    if not blockers:
        context_parts = []
        if environment:
            context_parts.append(f"env={environment}")
        if services:
            context_parts.append(f"services={', '.join(services)}")
        if window_start and window_end:
            context_parts.append(f"window={window_start}~{window_end}")
        ctx = " ".join(context_parts) or "(no filter)"
        print(f"No active locks block this operation ({ctx}).")
        return CommandResult(exit_code=EXIT_OK.code, run_id="")

    print(f"\nOperation BLOCKED by {len(blockers)} active lock(s):\n")
    for b in blockers:
        print(f"  [{b.scope.value.upper()}] {b.lock_id}")
        print(f"    Description : {b.description_short()}")
        print(f"    Created by  : {b.created_by}")
        if b.reason:
            print(f"    Reason      : {b.reason}")
        print()
    return CommandResult(
        exit_code=EXIT_LOCK_BLOCKED_OPERATION.code,
        run_id="",
        extra_artifacts={"blocking_locks": [b.to_dict() for b in blockers]},
    )


def _log_permission_denied(
    action: str,
    role: str,
    base: Optional[str] = None,
    actor: Optional[str] = None,
    lock_id: Optional[str] = None,
    lock_owner: Optional[str] = None,
) -> None:
    """Append a permission-denied record to the lock operations log."""
    import getpass
    import json
    from ..utils.storage import get_work_dir, LOCK_HISTORY_FILE, now_iso
    work = get_work_dir(base)
    log_path = work / LOCK_HISTORY_FILE
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        entry = {
            "timestamp": now_iso(),
            "action": "permission_denied",
            "lock_id": lock_id,
            "user": actor or getpass.getuser() or "unknown",
            "extra": {
                "attempted_action": action,
                "role": role,
                "lock_owner": lock_owner,
            },
        }
        with log_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception:
        pass
