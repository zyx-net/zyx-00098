"""`validate` command - run validation suite against a manifest."""
from __future__ import annotations

import argparse
from typing import Any

from .base import CommandResult
from ..core.models import Severity
from ..core.validator import ValidationEngine
from ..utils.exit_codes import EXIT_CONFIG_ERROR, EXIT_FILE_NOT_FOUND
from ..utils.logger import get_logger
from ..utils.policy_loader import PolicyValidationError, load_policy
from ..utils.storage import load_manifest

LOG = get_logger()
MODULE = "cmd.validate"


def add_parser(subparsers: "argparse._SubParsersAction") -> None:
    p = subparsers.add_parser("validate", help="Validate a release manifest")
    p.add_argument("-m", "--manifest", default="examples/sample_manifest.json",
                   help="Path to manifest JSON")
    p.add_argument("--policy", default=None,
                   help="Path to release policy JSON file (default: workspace release_policy.json or built-in)")
    p.add_argument("--no-checksum", action="store_true",
                   help="Skip file checksum verification")
    p.add_argument("--strict", action="store_true",
                   help="Treat warnings as errors (non-zero exit)")
    p.set_defaults(func=_run)


def _run(args: argparse.Namespace, **_: Any) -> CommandResult:
    try:
        manifest = load_manifest(args.manifest)
    except FileNotFoundError:
        LOG.error(MODULE, f"Manifest not found: {args.manifest}")
        print(f"ERROR: Manifest not found: {args.manifest}")
        return CommandResult(exit_code=EXIT_FILE_NOT_FOUND.code, run_id="")
    except ValueError as exc:
        LOG.error(MODULE, f"Invalid manifest: {exc}")
        print(f"ERROR: Invalid manifest: {exc}")
        return CommandResult(exit_code=EXIT_CONFIG_ERROR.code, run_id="")

    try:
        policy = load_policy(args.policy, work_dir=getattr(args, "work_dir", None))
    except FileNotFoundError as exc:
        LOG.error(MODULE, str(exc))
        print(f"ERROR: {exc}")
        return CommandResult(exit_code=EXIT_FILE_NOT_FOUND.code, run_id="")
    except PolicyValidationError as exc:
        LOG.error(MODULE, f"Invalid policy: {exc}")
        print(f"ERROR: Invalid policy: {exc}")
        for err in exc.errors:
            print(f"  - {err}")
        return CommandResult(exit_code=EXIT_CONFIG_ERROR.code, run_id="")

    policy_dict = policy.to_dict()

    engine = ValidationEngine(manifest, policy=policy)
    result = engine.validate(verify_checksums=not args.no_checksum)
    vr_dict = result.to_dict()
    summary = vr_dict["summary"]

    print(f"\n=== Validation Report ===")
    print(f"Release ID : {manifest.release_id}")
    print(f"Components : {len(manifest.components)}")
    print(f"Target Env : {manifest.target_environment.value}")
    print(f"Policy     : {policy.policy_version} (env rules: {', '.join(policy.list_known_environments())})")
    print(f"Timestamp  : {result.timestamp}")
    print(f"Total      : {summary['total']} issues")
    print(f"  Errors   : {summary['errors']}")
    print(f"  Warnings : {summary['warnings']}")
    print(f"  Infos    : {summary['infos']}")
    print(f"Result     : {'PASSED' if result.passed else 'FAILED'}")
    print()

    groups = {"ERROR": [], "WARNING": [], "INFO": []}
    for issue in result.issues:
        groups[issue.severity.value.upper()].append(issue)

    for level in ["ERROR", "WARNING", "INFO"]:
        items = groups[level]
        if not items:
            continue
        print(f"--- {level} ({len(items)}) ---")
        for i in items:
            comp = f"[{i.component}]" if i.component else ""
            print(f"  * {i.issue_code:25s} {comp:30s} {i.message}")
        print()

    exit_code = engine.determine_exit_code()
    if args.strict and exit_code == 0 and summary["warnings"] > 0:
        exit_code = 12
    return CommandResult(
        exit_code=exit_code,
        run_id="",
        manifest_snapshot=manifest.to_dict(),
        validation_result=vr_dict,
        extra_artifacts={"policy_snapshot": policy_dict},
    )
