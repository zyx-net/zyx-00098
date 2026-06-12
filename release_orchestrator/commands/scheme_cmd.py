"""`scheme` command - manage named release scheduling schemes.

Allows saving, loading, listing, deleting, importing, and exporting
scheduling configurations as named schemes for reuse across team members
and process restarts.
"""
from __future__ import annotations

import argparse
import json
import os
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional

from .base import CommandResult
from ..core.models import (
    ReleaseManifest,
    ReleaseScheme,
    ReleaseWindow,
    Wave,
    generate_id,
    now_iso,
)
from ..core.scheme_validator import SchemeValidationError, validate_scheme
from ..core.scheduler import (
    load_waves_from_json,
    load_windows_from_csv,
    load_windows_from_json,
    load_window_state,
)
from ..utils.exit_codes import (
    EXIT_CONFIG_ERROR,
    EXIT_FILE_NOT_FOUND,
    EXIT_OK,
    EXIT_SCHEME_ALREADY_EXISTS,
    EXIT_SCHEME_IO_ERROR,
    EXIT_SCHEME_NOT_FOUND,
    EXIT_SCHEME_VALIDATION_FAILED,
)
from ..utils.logger import get_logger
from ..utils.storage import (
    delete_scheme,
    export_scheme_to_file,
    import_scheme_from_file,
    list_schemes,
    load_scheme,
    save_scheme,
    scheme_exists,
)

LOG = get_logger()
MODULE = "cmd.scheme"


def add_parser(subparsers: "argparse._SubParsersAction") -> None:
    p = subparsers.add_parser(
        "scheme",
        help="Manage named release scheduling schemes",
        description="Save, load, list, delete, import, and export scheduling schemes.",
    )
    sub = p.add_subparsers(dest="scheme_command", metavar="<action>")

    # scheme save
    p_save = sub.add_parser(
        "save",
        help="Save a new scheme or overwrite an existing one",
    )
    p_save.add_argument("name", help="Name for the scheme")
    p_save.add_argument(
        "-m", "--manifest",
        default=None,
        help="Path to manifest JSON to include in the scheme",
    )
    p_save.add_argument(
        "-w", "--windows",
        default=None,
        help="Path to windows configuration (JSON or CSV)",
    )
    p_save.add_argument(
        "--waves", default=None,
        help="Path to waves configuration JSON",
    )
    p_save.add_argument(
        "--policy", default=None,
        help="Path to release policy JSON file",
    )
    p_save.add_argument(
        "-d", "--description", default=None,
        help="Description for this scheme",
    )
    p_save.add_argument(
        "--by", default="admin@corp.com",
        help="User saving the scheme",
    )
    p_save.add_argument(
        "--tag", action="append", dest="tags", default=[],
        help="Add a tag to the scheme (may be specified multiple times)",
    )
    p_save.add_argument(
        "--force", "--overwrite", action="store_true", dest="overwrite",
        help="Overwrite an existing scheme with the same name (default: reject)",
    )
    p_save.add_argument(
        "--skip-validation", action="store_true",
        help="Skip scheme validation (not recommended)",
    )

    # scheme load
    p_load = sub.add_parser(
        "load",
        help="Load and display a scheme by name",
    )
    p_load.add_argument("name", help="Name of the scheme to load")
    p_load.add_argument(
        "--export-manifest", default=None, metavar="PATH",
        help="Export the scheme's manifest to a JSON file",
    )
    p_load.add_argument(
        "--export-windows", default=None, metavar="PATH",
        help="Export the scheme's windows to a JSON file",
    )
    p_load.add_argument(
        "--export-waves", default=None, metavar="PATH",
        help="Export the scheme's waves to a JSON file",
    )
    p_load.add_argument(
        "--json", action="store_true", dest="as_json",
        help="Output as JSON instead of human-readable text",
    )

    # scheme list
    p_list = sub.add_parser(
        "list",
        help="List all saved schemes",
    )
    p_list.add_argument(
        "--json", action="store_true", dest="as_json",
        help="Output as JSON instead of human-readable text",
    )

    # scheme delete
    p_delete = sub.add_parser(
        "delete",
        help="Delete a scheme by name",
    )
    p_delete.add_argument("name", help="Name of the scheme to delete")
    p_delete.add_argument(
        "-f", "--force", action="store_true",
        help="Don't ask for confirmation",
    )

    # scheme import
    p_import = sub.add_parser(
        "import",
        help="Import a scheme from an external JSON file",
    )
    p_import.add_argument("file", help="Path to the scheme JSON file to import")
    p_import.add_argument(
        "-n", "--name", default=None,
        help="Override the scheme name (default: use name from file)",
    )
    p_import.add_argument(
        "--force", "--overwrite", action="store_true", dest="overwrite",
        help="Overwrite an existing scheme with the same name",
    )

    # scheme export
    p_export = sub.add_parser(
        "export",
        help="Export a scheme to an external JSON file",
    )
    p_export.add_argument("name", help="Name of the scheme to export")
    p_export.add_argument(
        "-o", "--output", required=True,
        help="Path to write the exported scheme JSON",
    )

    p.set_defaults(func=_run)


def _run(args: argparse.Namespace, **_: Any) -> CommandResult:
    base = getattr(args, "work_dir", None)
    cmd = getattr(args, "scheme_command", None)

    if not cmd:
        print("ERROR: No scheme action specified. Use one of: save, load, list, delete, import, export")
        return CommandResult(exit_code=EXIT_CONFIG_ERROR.code, run_id="")

    handlers = {
        "save": _handle_save,
        "load": _handle_load,
        "list": _handle_list,
        "delete": _handle_delete,
        "import": _handle_import,
        "export": _handle_export,
    }

    handler = handlers.get(cmd)
    if not handler:
        print(f"ERROR: Unknown scheme action: {cmd}")
        return CommandResult(exit_code=EXIT_CONFIG_ERROR.code, run_id="")

    return handler(args, base)


def _handle_save(args: argparse.Namespace, base: Optional[str]) -> CommandResult:
    scheme_name = args.name
    existing = scheme_exists(scheme_name, base)
    if existing and not args.overwrite:
        print(f"ERROR: Scheme '{scheme_name}' already exists. Use --force to overwrite.")
        LOG.error(MODULE, f"Scheme '{scheme_name}' already exists (use --force to overwrite)")
        return CommandResult(exit_code=EXIT_SCHEME_ALREADY_EXISTS.code, run_id="")

    try:
        scheme = _build_scheme_from_args(args)
    except (FileNotFoundError, ValueError) as exc:
        print(f"ERROR: {exc}")
        LOG.error(MODULE, f"Failed to build scheme: {exc}")
        if isinstance(exc, FileNotFoundError):
            return CommandResult(exit_code=EXIT_FILE_NOT_FOUND.code, run_id="")
        return CommandResult(exit_code=EXIT_CONFIG_ERROR.code, run_id="")

    if not args.skip_validation:
        existing_locks = _load_existing_locks(args, base)
        passed, issues = validate_scheme(scheme, existing_locks)
        errors = [i for i in issues if i.get("severity") == "error"]

        if errors:
            print(f"\nERROR: Scheme validation failed with {len(errors)} error(s):")
            for issue in issues:
                sev = issue["severity"].upper()
                print(f"  [{sev}] {issue['issue_code']}: {issue['message']}")
            LOG.error(
                MODULE,
                f"Scheme validation failed for '{scheme_name}'",
                errors=len(errors),
            )
            return CommandResult(
                exit_code=EXIT_SCHEME_VALIDATION_FAILED.code,
                run_id="",
                extra_artifacts={"scheme": scheme.to_dict(), "validation_issues": issues},
            )

        if issues:
            warnings = [i for i in issues if i.get("severity") == "warning"]
            if warnings:
                print(f"\nValidation warnings ({len(warnings)}):")
                for w in warnings:
                    print(f"  [WARNING] {w['issue_code']}: {w['message']}")

    if args.overwrite and existing:
        scheme.updated_at = now_iso()
        LOG.info(MODULE, f"Overwriting existing scheme '{scheme_name}'")

    try:
        save_scheme(scheme, base=base, overwrite=args.overwrite)
    except FileExistsError:
        print(f"ERROR: Scheme '{scheme_name}' already exists. Use --force to overwrite.")
        return CommandResult(exit_code=EXIT_SCHEME_ALREADY_EXISTS.code, run_id="")
    except IOError as exc:
        print(f"ERROR: Failed to save scheme: {exc}")
        LOG.error(MODULE, f"Save scheme IO error: {exc}")
        return CommandResult(exit_code=EXIT_SCHEME_IO_ERROR.code, run_id="")

    action = "Overwritten" if existing and args.overwrite else "Saved"
    print(f"\n{action} scheme: {scheme_name}")
    print(f"  Created by   : {scheme.created_by}")
    print(f"  Created at   : {scheme.created_at}")
    if scheme.updated_at:
        print(f"  Updated at   : {scheme.updated_at}")
    print(f"  Windows      : {len(scheme.release_windows)}")
    print(f"  Waves        : {len(scheme.waves)}")
    if scheme.manifest:
        print(f"  Manifest     : {scheme.manifest.get('release_id', '(embedded)')}")
    if scheme.description:
        print(f"  Description  : {scheme.description}")
    if scheme.tags:
        print(f"  Tags         : {', '.join(scheme.tags)}")

    return CommandResult(
        exit_code=EXIT_OK.code,
        run_id="",
        extra_artifacts={"scheme": scheme.to_dict()},
    )


def _handle_load(args: argparse.Namespace, base: Optional[str]) -> CommandResult:
    scheme_name = args.name
    try:
        scheme = load_scheme(scheme_name, base)
    except FileNotFoundError:
        print(f"ERROR: Scheme not found: '{scheme_name}'")
        return CommandResult(exit_code=EXIT_SCHEME_NOT_FOUND.code, run_id="")
    except ValueError as exc:
        print(f"ERROR: Failed to load scheme: {exc}")
        return CommandResult(exit_code=EXIT_SCHEME_VALIDATION_FAILED.code, run_id="")
    except IOError as exc:
        print(f"ERROR: {exc}")
        return CommandResult(exit_code=EXIT_SCHEME_IO_ERROR.code, run_id="")

    # Export individual parts if requested
    if args.export_manifest and scheme.manifest:
        try:
            out = Path(args.export_manifest)
            out.parent.mkdir(parents=True, exist_ok=True)
            with out.open("w", encoding="utf-8") as f:
                json.dump(scheme.manifest, f, indent=2, ensure_ascii=False)
            print(f"Manifest written to: {args.export_manifest}")
        except IOError as exc:
            print(f"ERROR: Failed to write manifest: {exc}")
            return CommandResult(exit_code=EXIT_SCHEME_IO_ERROR.code, run_id="")

    if args.export_windows and scheme.release_windows:
        try:
            out = Path(args.export_windows)
            out.parent.mkdir(parents=True, exist_ok=True)
            data = {"windows": [w.to_dict() for w in scheme.release_windows]}
            with out.open("w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
            print(f"Windows written to: {args.export_windows}")
        except IOError as exc:
            print(f"ERROR: Failed to write windows: {exc}")
            return CommandResult(exit_code=EXIT_SCHEME_IO_ERROR.code, run_id="")

    if args.export_waves and scheme.waves:
        try:
            out = Path(args.export_waves)
            out.parent.mkdir(parents=True, exist_ok=True)
            data = {"waves": [w.to_dict() for w in scheme.waves]}
            with out.open("w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
            print(f"Waves written to: {args.export_waves}")
        except IOError as exc:
            print(f"ERROR: Failed to write waves: {exc}")
            return CommandResult(exit_code=EXIT_SCHEME_IO_ERROR.code, run_id="")

    if args.as_json:
        print(json.dumps(scheme.to_dict(), indent=2, ensure_ascii=False, default=str))
        return CommandResult(exit_code=EXIT_OK.code, run_id="")

    _print_scheme_details(scheme)
    return CommandResult(
        exit_code=EXIT_OK.code,
        run_id="",
        extra_artifacts={"scheme": scheme.to_dict()},
    )


def _handle_list(args: argparse.Namespace, base: Optional[str]) -> CommandResult:
    schemes = list_schemes(base)

    if args.as_json:
        print(json.dumps(schemes, indent=2, ensure_ascii=False, default=str))
        return CommandResult(exit_code=EXIT_OK.code, run_id="")

    if not schemes:
        print("No saved schemes found. Use 'scheme save' to create one.")
        return CommandResult(exit_code=EXIT_OK.code, run_id="")

    print(f"\n=== Saved Schemes ({len(schemes)}) ===")
    print(f"{'Name':25s} {'Windows':>8s} {'Waves':>6s} {'Created By':20s} {'Created At':25s} {'Updated At':25s}")
    print("-" * 120)
    for s in schemes:
        updated = s.get("updated_at") or "-"
        print(
            f"{s['name']:25s} "
            f"{s.get('windows_count', 0):>8d} "
            f"{s.get('waves_count', 0):>6d} "
            f"{s.get('created_by', 'unknown'):20s} "
            f"{s.get('created_at', '-'):25s} "
            f"{updated:25s}"
        )
    print(f"\nTip: use 'scheme load <name>' to inspect a scheme.")
    print(f"Tip: use 'scheme save --help' to see how to create a scheme.")

    return CommandResult(exit_code=EXIT_OK.code, run_id="")


def _handle_delete(args: argparse.Namespace, base: Optional[str]) -> CommandResult:
    scheme_name = args.name

    if not scheme_exists(scheme_name, base):
        print(f"ERROR: Scheme not found: '{scheme_name}'")
        return CommandResult(exit_code=EXIT_SCHEME_NOT_FOUND.code, run_id="")

    if not args.force:
        resp = input(f"Are you sure you want to delete scheme '{scheme_name}'? [y/N]: ")
        if resp.lower() not in ("y", "yes"):
            print("Delete cancelled.")
            return CommandResult(exit_code=EXIT_OK.code, run_id="")

    try:
        ok = delete_scheme(scheme_name, base)
    except IOError as exc:
        print(f"ERROR: Failed to delete scheme: {exc}")
        return CommandResult(exit_code=EXIT_SCHEME_IO_ERROR.code, run_id="")

    if ok:
        print(f"Deleted scheme: {scheme_name}")
        return CommandResult(exit_code=EXIT_OK.code, run_id="")
    else:
        print(f"ERROR: Scheme not found: '{scheme_name}'")
        return CommandResult(exit_code=EXIT_SCHEME_NOT_FOUND.code, run_id="")


def _handle_import(args: argparse.Namespace, base: Optional[str]) -> CommandResult:
    file_path = args.file
    scheme_name = args.name
    overwrite = args.overwrite

    if scheme_name:
        if scheme_exists(scheme_name, base) and not overwrite:
            print(f"ERROR: Scheme '{scheme_name}' already exists. Use --force to overwrite.")
            return CommandResult(exit_code=EXIT_SCHEME_ALREADY_EXISTS.code, run_id="")

    try:
        scheme = import_scheme_from_file(
            file_path,
            scheme_name=scheme_name,
            base=base,
            overwrite=overwrite,
        )
    except FileNotFoundError:
        print(f"ERROR: File not found: {file_path}")
        return CommandResult(exit_code=EXIT_FILE_NOT_FOUND.code, run_id="")
    except ValueError as exc:
        print(f"ERROR: {exc}")
        return CommandResult(exit_code=EXIT_SCHEME_VALIDATION_FAILED.code, run_id="")
    except FileExistsError:
        name = scheme_name or "(from file)"
        print(f"ERROR: Scheme '{name}' already exists. Use --force to overwrite.")
        return CommandResult(exit_code=EXIT_SCHEME_ALREADY_EXISTS.code, run_id="")
    except IOError as exc:
        print(f"ERROR: {exc}")
        return CommandResult(exit_code=EXIT_SCHEME_IO_ERROR.code, run_id="")

    print(f"\nImported scheme: {scheme.scheme_name}")
    print(f"  Windows      : {len(scheme.release_windows)}")
    print(f"  Waves        : {len(scheme.waves)}")
    return CommandResult(
        exit_code=EXIT_OK.code,
        run_id="",
        extra_artifacts={"scheme": scheme.to_dict()},
    )


def _handle_export(args: argparse.Namespace, base: Optional[str]) -> CommandResult:
    scheme_name = args.name
    output_path = args.output

    try:
        path = export_scheme_to_file(scheme_name, output_path, base)
    except FileNotFoundError:
        print(f"ERROR: Scheme not found: '{scheme_name}'")
        return CommandResult(exit_code=EXIT_SCHEME_NOT_FOUND.code, run_id="")
    except IOError as exc:
        print(f"ERROR: {exc}")
        return CommandResult(exit_code=EXIT_SCHEME_IO_ERROR.code, run_id="")

    print(f"Exported scheme '{scheme_name}' to: {path}")
    return CommandResult(exit_code=EXIT_OK.code, run_id="")


def _build_scheme_from_args(args: argparse.Namespace) -> ReleaseScheme:
    windows: List[ReleaseWindow] = []
    waves: List[Wave] = []
    manifest_dict: Optional[Dict[str, Any]] = None
    manifest_path: Optional[str] = None
    policy_path: Optional[str] = None
    policy_dict: Optional[Dict[str, Any]] = None

    # Load manifest if provided
    if args.manifest:
        from ..utils.storage import load_manifest
        manifest_path = args.manifest
        manifest = load_manifest(args.manifest)
        manifest_dict = manifest.to_dict()
        if manifest.release_windows and not args.windows:
            windows = list(manifest.release_windows)
        if manifest.waves and not args.waves:
            waves = list(manifest.waves)

    # Load windows if provided
    if args.windows:
        if args.windows.lower().endswith(".csv"):
            windows = load_windows_from_csv(args.windows)
        else:
            windows = load_windows_from_json(args.windows)

    # Load waves if provided
    if args.waves:
        waves = load_waves_from_json(args.waves)

    # Load policy if provided
    if args.policy:
        policy_path = args.policy
        with open(args.policy, "r", encoding="utf-8") as f:
            policy_dict = json.load(f)

    if not windows and not manifest_dict:
        raise ValueError(
            "No windows configuration found. Provide --windows or a --manifest with embedded release_windows."
        )

    return ReleaseScheme(
        scheme_name=args.name,
        created_at=now_iso(),
        created_by=getattr(args, "by", "admin@corp.com"),
        description=getattr(args, "description", None),
        manifest=manifest_dict,
        manifest_path=manifest_path,
        release_windows=windows,
        waves=waves,
        policy_path=policy_path,
        policy=policy_dict,
        windows_config_path=getattr(args, "windows", None),
        waves_config_path=getattr(args, "waves", None),
        tags=list(getattr(args, "tags", [])),
        metadata={"source": "cli_scheme_save"},
    )


def _load_existing_locks(args: argparse.Namespace, base: Optional[str]) -> Dict[str, Any]:
    """Load existing window lock state for validation."""
    from ..core.scheduler import WINDOW_STATE_FILE
    from ..utils.storage import get_work_dir, load_json

    work = get_work_dir(base)
    state_file = work / WINDOW_STATE_FILE
    if not state_file.exists():
        return {}
    try:
        return load_json(state_file)
    except Exception:
        return {}


def _print_scheme_details(scheme: ReleaseScheme) -> None:
    print(f"\n=== Scheme: {scheme.scheme_name} ===")
    print(f"Created by   : {scheme.created_by}")
    print(f"Created at   : {scheme.created_at}")
    if scheme.updated_at:
        print(f"Updated at   : {scheme.updated_at}")
    if scheme.description:
        print(f"Description  : {scheme.description}")
    if scheme.tags:
        print(f"Tags         : {', '.join(scheme.tags)}")
    if scheme.manifest_path:
        print(f"Manifest path: {scheme.manifest_path}")
    if scheme.windows_config_path:
        print(f"Windows path : {scheme.windows_config_path}")
    if scheme.waves_config_path:
        print(f"Waves path   : {scheme.waves_config_path}")
    if scheme.policy_path:
        print(f"Policy path  : {scheme.policy_path}")

    if scheme.manifest:
        mid = scheme.manifest.get("release_id", "(unknown)")
        env = scheme.manifest.get("target_environment", "(unknown)")
        comps = len(scheme.manifest.get("components", []))
        print(f"\nManifest     : {mid} (env={env}, components={comps})")

    if scheme.release_windows:
        print(f"\nWindows ({len(scheme.release_windows)}):")
        for w in scheme.release_windows:
            status = "LOCKED" if w.locked else "OPEN"
            cap = f" (capacity: {w.capacity_max})" if w.capacity_max else ""
            envs = f" [{', '.join(w.allowed_environments)}]" if w.allowed_environments else ""
            print(
                f"  [{status}] {w.name} ({w.window_id}): "
                f"{w.start_time} -> {w.end_time}{cap}{envs}"
            )

    if scheme.waves:
        print(f"\nWaves ({len(scheme.waves)}):")
        for wave in sorted(scheme.waves, key=lambda w: w.order):
            desc = f" - {wave.description}" if wave.description else ""
            print(f"  [{wave.order}] {wave.name} ({wave.wave_id}){desc}")
