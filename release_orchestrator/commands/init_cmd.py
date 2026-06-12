"""`init` command - generate sample manifest and artifact files."""
from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any, Dict

from .base import CommandResult
from ..core.models import EnvironmentType
from ..core.samples import generate_sample_manifest
from ..utils.exit_codes import EXIT_CONFIG_ERROR, EXIT_OK
from ..utils.logger import get_logger

LOG = get_logger()
MODULE = "cmd.init"


def add_parser(subparsers: "argparse._SubParsersAction") -> None:
    p = subparsers.add_parser("init", help="Generate a sample manifest and artifact files")
    p.add_argument("-o", "--output", default="examples/sample_manifest.json",
                   help="Output manifest path (default: examples/sample_manifest.json)")
    p.add_argument("--clean", action="store_true",
                   help="Remove existing output path before generating")
    p.add_argument("--no-errors", action="store_true",
                   help="Generate a manifest without intentional validation errors")
    p.add_argument("--env", default="production",
                   choices=["dev", "test", "staging", "production"],
                   help="Target environment for the sample manifest")
    p.set_defaults(func=_run)


def _run(args: argparse.Namespace, **_: Any) -> CommandResult:
    output = Path(args.output)
    if args.clean and output.exists():
        try:
            output.unlink()
        except OSError:
            pass
    if output.exists():
        LOG.warning(MODULE, f"Output already exists: {output} (use --clean to overwrite)")

    env = EnvironmentType(args.env)
    include_errors = not args.no_errors
    try:
        manifest, path = generate_sample_manifest(
            output_path=str(output),
            include_errors=include_errors,
            target_env=env,
        )
    except Exception as exc:
        LOG.error(MODULE, f"Failed to generate sample manifest: {exc}")
        return CommandResult(
            exit_code=EXIT_CONFIG_ERROR.code,
            run_id="",
            manifest_snapshot={"error": str(exc)},
        )

    LOG.info(
        MODULE,
        "Sample manifest generated successfully",
        path=str(path),
        components=len(manifest.components),
        artifacts_dir=str(path.parent / "artifacts"),
    )
    print(f"\nSample manifest written to: {path}")
    print(f"Components: {len(manifest.components)}")
    if include_errors:
        print("Note: includes intentional errors for validation testing.")
        print("  - legacy-reporting: version downgrade + missing approval + bad checksum")
    return CommandResult(
        exit_code=EXIT_OK.code,
        run_id="",
        manifest_snapshot=manifest.to_dict(),
        extra_artifacts={
            "generated_path.txt": str(path),
        },
    )
