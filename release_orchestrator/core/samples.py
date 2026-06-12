"""Example / sample manifest generator for the `init` command.

Produces a realistic but self-contained sample manifest file with
multiple components, dependency chains, approvals, and some
deliberately broken scenarios so users can experiment with the
validator's error conditions.
"""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from ..core.models import (
    ApprovalRecord,
    ApprovalStatus,
    Component,
    Dependency,
    EnvironmentType,
    PackageArtifact,
    ReleaseManifest,
    compute_checksum,
    generate_id,
    now_iso,
)
from ..utils.logger import get_logger

LOG = get_logger()
MODULE = "samples"


def _fake_package_bytes(name: str, version: str) -> bytes:
    """Generate deterministic fake package content."""
    return f"PACKAGE:{name}:{version}::{os.urandom(0).hex()}".encode("utf-8")


def _approval(approver: str, status: ApprovalStatus = ApprovalStatus.APPROVED, days_ago: int = 2) -> ApprovalRecord:
    ts = (datetime.utcnow() - timedelta(days=days_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")
    return ApprovalRecord(
        approver=approver,
        status=status,
        timestamp=ts,
        comment=f"Approved by {approver} via offline review.",
    )


def generate_sample_manifest(
    output_path: str,
    include_errors: bool = True,
    target_env: EnvironmentType = EnvironmentType.PRODUCTION,
) -> Tuple[ReleaseManifest, Path]:
    """Generate a complete sample manifest + fake artifact files.

    Args:
        output_path: Where to write the manifest JSON.
        include_errors: If True, include components that exercise
            the validator's error conditions (version downgrade,
            circular dep, missing approval, bad checksum).
        target_env: Target environment for the manifest.

    Returns:
        Tuple of (ReleaseManifest object, manifest file path).
    """
    out = Path(output_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    artifacts_dir = out.parent / "artifacts"
    artifacts_dir.mkdir(exist_ok=True)

    def _make_artifact(name: str, version: str, bad_checksum: bool = False) -> PackageArtifact:
        content = _fake_package_bytes(name, version)
        path = artifacts_dir / f"{name}-{version}.pkg"
        path.write_bytes(content)
        declared = compute_checksum(content)
        if bad_checksum:
            declared = hashlib.sha256(b"WRONG").hexdigest()
        return PackageArtifact(
            path=str(path),
            checksum=declared,
            checksum_algorithm="sha256",
            size_bytes=len(content),
        )

    components: List[Component] = []

    components.append(Component(
        name="common-utils",
        version="2.3.1",
        environment=target_env,
        artifact=_make_artifact("common-utils", "2.3.1"),
        dependencies=[],
        approvals=[_approval("alice@corp.com", days_ago=5)],
        deployed_version="2.2.0",
        description="Shared utilities library - base dependency for all services.",
        rollback_target_version="2.2.0",
        tags=["shared", "libs"],
    ))

    components.append(Component(
        name="auth-service",
        version="1.5.0",
        environment=target_env,
        artifact=_make_artifact("auth-service", "1.5.0"),
        dependencies=[
            Dependency(name="common-utils", min_version="2.3.0", required=True),
        ],
        approvals=[_approval("bob@corp.com", days_ago=3)],
        deployed_version="1.4.2",
        description="Authentication and SSO service.",
        rollback_target_version="1.4.2",
        tags=["core", "security"],
    ))

    components.append(Component(
        name="order-service",
        version="3.1.0",
        environment=target_env,
        artifact=_make_artifact("order-service", "3.1.0"),
        dependencies=[
            Dependency(name="common-utils", min_version="2.3.0"),
            Dependency(name="auth-service", min_version="1.5.0"),
        ],
        approvals=[_approval("carol@corp.com", days_ago=1)],
        deployed_version="3.0.4",
        description="Order management micro-service.",
        rollback_target_version="3.0.4",
        tags=["business"],
    ))

    components.append(Component(
        name="payment-service",
        version="2.0.2",
        environment=target_env,
        artifact=_make_artifact("payment-service", "2.0.2"),
        dependencies=[
            Dependency(name="common-utils", min_version="2.2.0"),
            Dependency(name="order-service", min_version="3.0.0"),
        ],
        approvals=[_approval("dave@corp.com", days_ago=1)],
        deployed_version="2.0.1",
        description="Payment processing service (PCI-DSS compliant).",
        rollback_target_version="2.0.1",
        tags=["pci", "payment"],
    ))

    if include_errors:
        components.append(Component(
            name="legacy-reporting",
            version="1.0.3",
            environment=target_env,
            artifact=_make_artifact("legacy-reporting", "1.0.3", bad_checksum=True),
            dependencies=[Dependency(name="common-utils", min_version="1.0.0")],
            approvals=[],
            deployed_version="1.1.0",
            description="(INTENTIONAL ERROR) Version downgrade + missing approval + bad checksum.",
            rollback_target_version="1.0.9",
            tags=["legacy", "error-demo"],
        ))

    manifest = ReleaseManifest(
        manifest_version="1.0",
        release_id=generate_id("REL"),
        title=f"Q{datetime.utcnow().month//3 + 1} Offline Release Bundle",
        target_environment=target_env,
        scheduled_date=(datetime.utcnow() + timedelta(days=1)).strftime("%Y-%m-%d"),
        components=components,
        description="Sample offline release manifest generated by `release-orchestrator init`.",
        created_by="release-orchestrator",
        created_at=now_iso(),
    )

    out.write_text(manifest.to_json(), encoding="utf-8")
    LOG.info(
        MODULE,
        "Sample manifest generated",
        path=str(out),
        components=len(components),
        artifacts_dir=str(artifacts_dir),
    )
    return manifest, out
