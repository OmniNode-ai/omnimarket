# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""NodeVolumeConfigDriftSweep — volume-vs-packaged config drift detection.

Compares the deployed (Docker named volume) copy of a runtime-rendered config
against the packaged source shipped in the image, and classifies drift. The
Bifrost delegation contract is rendered once to ``/app/data/delegation/`` and the
volume survives rebuilds, so the deployed copy silently diverges from packaged
source (OMN-12958 / OMN-12945).

Two input modes:

* **sidecar** — read the provenance sidecar JSON written by the runtime at
  ``/app/data/delegation/.config_provenance.json`` (probed off a runtime lane).
  This is the deployment path: the runtime already computed the sha pair.
* **paths** — compute provenance directly from a deployed-copy path and a
  source path (used in dry-run / local verification, and when no sidecar exists).

ONEX node type: COMPUTE — deterministic classification, no LLM calls. The node
classifies drift and emits a recommended-ticket payload per drifted lane; the
invoking skill/orchestrator owns Linear ticket creation (same split as
node_database_sweep).
"""

from __future__ import annotations

import hashlib
import importlib.resources
import json
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

# Default deployed contract location inside the runtime container volume.
DEFAULT_DEPLOYED_PATH = "/app/data/delegation/bifrost_delegation.yaml"
# Sidecar written by the runtime next to the deployed contract (OMN-12958).
PROVENANCE_SIDECAR_NAME = ".config_provenance.json"

# Drift classifications.
STATUS_IN_SYNC = "IN_SYNC"
STATUS_DRIFTED = "DRIFTED"
STATUS_DEPLOYED_ABSENT = "DEPLOYED_ABSENT"
STATUS_SOURCE_ABSENT = "SOURCE_ABSENT"


def _sha256(path: Path) -> str | None:
    if not path.exists():
        return None
    digest = hashlib.sha256()
    digest.update(path.read_bytes())
    return digest.hexdigest()


def _packaged_source_path() -> Path:
    """Resolve the packaged Bifrost delegation source (canonical: omnimarket)."""
    ref = importlib.resources.files("omnimarket").joinpath(
        "configs/bifrost_delegation.yaml"
    )
    return Path(str(ref))


class ModelDriftFinding(BaseModel):
    """Drift classification for a single config on a single lane/source."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    config_name: str
    lane: str = Field(default="local")
    status: str  # IN_SYNC | DRIFTED | DEPLOYED_ABSENT | SOURCE_ABSENT
    deployed_path: str
    deployed_sha256: str | None = None
    source_path: str
    source_sha256: str | None = None
    message: str = ""

    @property
    def is_drift(self) -> bool:
        return self.status == STATUS_DRIFTED

    def recommended_ticket_title(self) -> str:
        return (
            f"Volume config drift: {self.config_name} on lane '{self.lane}' "
            "diverged from packaged source — re-seed required (OMN-12958)"
        )

    def recommended_ticket_body(self) -> str:
        return (
            f"The deployed (volume) copy of `{self.config_name}` on lane "
            f"`{self.lane}` has drifted from the packaged source.\n\n"
            f"- deployed_path: `{self.deployed_path}`\n"
            f"- deployed_sha256: `{self.deployed_sha256}`\n"
            f"- source_path: `{self.source_path}`\n"
            f"- source_sha256: `{self.source_sha256}`\n\n"
            "Follow the re-seed procedure in "
            "`omnibase_infra/docs/runbooks/volume-config-drift-and-reseed.md`: "
            "ledger the overlay diff, then re-seed from packaged source. "
            "Do not hand-edit the volume copy."
        )


class VolumeConfigDriftSweepRequest(BaseModel):
    """Input for the volume config drift sweep."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    config_name: str = Field(default="bifrost_delegation")
    lane: str = Field(default="local")
    # Mode 1: provenance sidecar JSON (deployment path).
    sidecar_path: str | None = None
    # Mode 2: explicit paths (dry-run / local).
    deployed_path: str | None = None
    source_path: str | None = None
    dry_run: bool = False


class VolumeConfigDriftSweepResult(BaseModel):
    """Output of the volume config drift sweep."""

    model_config = ConfigDict(extra="forbid")

    findings: list[ModelDriftFinding] = Field(default_factory=list)
    drift_count: int = 0
    status: str = "clean"  # clean | drift_found | error
    dry_run: bool = False


def _finding_from_paths(
    *,
    config_name: str,
    lane: str,
    deployed_path: Path,
    source_path: Path,
) -> ModelDriftFinding:
    deployed_sha = _sha256(deployed_path)
    source_sha = _sha256(source_path)

    if deployed_sha is None:
        status = STATUS_DEPLOYED_ABSENT
        message = f"deployed config absent at {deployed_path}"
    elif source_sha is None:
        status = STATUS_SOURCE_ABSENT
        message = f"packaged source absent at {source_path}"
    elif deployed_sha != source_sha:
        status = STATUS_DRIFTED
        message = "deployed volume copy diverged from packaged source; re-seed required"
    else:
        status = STATUS_IN_SYNC
        message = "deployed sha matches packaged source"

    return ModelDriftFinding(
        config_name=config_name,
        lane=lane,
        status=status,
        deployed_path=str(deployed_path),
        deployed_sha256=deployed_sha,
        source_path=str(source_path),
        source_sha256=source_sha,
        message=message,
    )


def _finding_from_sidecar(*, lane: str, sidecar_path: Path) -> ModelDriftFinding:
    raw = json.loads(sidecar_path.read_text(encoding="utf-8"))
    deployed_sha = raw.get("deployed_sha256")
    source_sha = raw.get("source_sha256")

    if deployed_sha is None:
        status = STATUS_DEPLOYED_ABSENT
        message = "sidecar reports deployed config absent"
    elif source_sha is None:
        status = STATUS_SOURCE_ABSENT
        message = "sidecar reports packaged source absent"
    elif deployed_sha != source_sha:
        status = STATUS_DRIFTED
        message = "sidecar reports drift; re-seed required"
    else:
        status = STATUS_IN_SYNC
        message = "sidecar reports in-sync"

    return ModelDriftFinding(
        config_name=str(raw.get("config_name", "bifrost_delegation")),
        lane=lane,
        status=status,
        deployed_path=str(raw.get("deployed_path", "")),
        deployed_sha256=deployed_sha,
        source_path=str(raw.get("source_path", "")),
        source_sha256=source_sha,
        message=message,
    )


class NodeVolumeConfigDriftSweep:
    """Classify volume-vs-packaged config drift for runtime-rendered contracts."""

    def handle(
        self, request: VolumeConfigDriftSweepRequest
    ) -> VolumeConfigDriftSweepResult:
        if request.sidecar_path:
            finding = _finding_from_sidecar(
                lane=request.lane,
                sidecar_path=Path(request.sidecar_path),
            )
        else:
            deployed = Path(request.deployed_path or DEFAULT_DEPLOYED_PATH)
            source = (
                Path(request.source_path)
                if request.source_path
                else _packaged_source_path()
            )
            finding = _finding_from_paths(
                config_name=request.config_name,
                lane=request.lane,
                deployed_path=deployed,
                source_path=source,
            )

        findings = [finding]
        drift_count = sum(1 for f in findings if f.is_drift)
        return VolumeConfigDriftSweepResult(
            findings=findings,
            drift_count=drift_count,
            status="drift_found" if drift_count else "clean",
            dry_run=request.dry_run,
        )


__all__ = [
    "DEFAULT_DEPLOYED_PATH",
    "PROVENANCE_SIDECAR_NAME",
    "ModelDriftFinding",
    "NodeVolumeConfigDriftSweep",
    "VolumeConfigDriftSweepRequest",
    "VolumeConfigDriftSweepResult",
]
