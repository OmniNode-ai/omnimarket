# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Fresh workspaces from pinned bytes, with no symlinks or hardlinks to sibling runs."""

from __future__ import annotations

import hashlib
import json
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from omnimarket.nodes.node_compaction_arm_run_effect.models.model_arm_run import (
    Arm,
    ModelArmRunRequest,
)


def sha(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def digest(value: object) -> str:
    return sha(json.dumps(value, sort_keys=True, separators=(",", ":")).encode())


def validate_inputs(request: ModelArmRunRequest) -> None:
    case = request.case
    if (
        len(request.transcript_prefix) != case.prefix_bytes
        or sha(request.transcript_prefix) != case.prefix_sha256
    ):
        raise ValueError("transcript prefix does not match the case pin")
    files = {f.name: f.content for f in request.ledger_files}
    if len(files) != len(request.ledger_files) or set(files) != {
        c.name for c in case.ledger_cuts
    }:
        raise ValueError("ledger files do not match the case cuts")
    for cut in case.ledger_cuts:
        content = files[cut.name]
        if len(content) != cut.prefix_bytes or sha(content) != cut.prefix_sha256:
            raise ValueError("ledger prefix does not match the case pin")
    if request.ground_truth is not None:
        truth = request.ground_truth
        rows = [
            row.strip()
            for file in request.ledger_files
            for row in file.content.decode().splitlines()
            if row.strip()
        ]
        if (
            truth.session_id != case.session_id
            or sha("\n".join(rows).encode()) != truth.prefix_hash
        ):
            raise ValueError("ground truth does not match the pinned ledger or session")
        if case.boundary_at is None or datetime.fromisoformat(
            truth.now
        ) != datetime.fromisoformat(case.boundary_at):
            raise ValueError("ground truth does not match the case boundary instant")
    summary = request.recorded_summary.encode()
    if (
        case.summary_check != "PINNED"
        or len(request.recorded_summary) != case.summary_length
        or sha(summary) != case.summary_sha256
    ):
        raise ValueError("recorded summary does not match the case pin")


@dataclass(frozen=True)
class ModelArmWorkspace:
    root: Path
    transcript: Path
    ledger_files: tuple[Path, ...]
    initial_state_hash: str
    prefix_hash: str
    ledger_hash: str

    def unchanged(self) -> bool:
        try:
            if self.transcript.is_symlink() or any(
                p.is_symlink() for p in self.ledger_files
            ):
                return False
            return self.prefix_hash == sha(
                self.transcript.read_bytes()
            ) and self.ledger_hash == digest(
                [[p.name, sha(p.read_bytes())] for p in self.ledger_files]
            )
        except OSError:
            return False


class HandlerForkWorkspace:
    def __init__(self, root: Path) -> None:
        self.root = root

    def fork(
        self, request: ModelArmRunRequest, arm: Arm, repeat: int
    ) -> ModelArmWorkspace:
        validate_inputs(request)
        self.root.mkdir(parents=True, exist_ok=True)
        # No caller-controlled file names become paths. Each fork is a private
        # directory, including retries at the same case/arm/repeat key.
        root = Path(tempfile.mkdtemp(prefix=f"{arm}-{repeat}-", dir=self.root))
        transcript = root / "prefix.jsonl"
        transcript.write_bytes(request.transcript_prefix)
        ledgers = root / "ledger"
        ledgers.mkdir()
        paths: list[Path] = []
        for i, file in enumerate(sorted(request.ledger_files, key=lambda f: f.name)):
            path = ledgers / f"{i:04d}.md"
            path.write_bytes(file.content)
            paths.append(path)
        prefix_hash = sha(transcript.read_bytes())
        ledger_hash = digest([[p.name, sha(p.read_bytes())] for p in paths])
        state_hash = digest(
            {
                "prefix": prefix_hash,
                "ledger": ledger_hash,
                "case": request.case.model_dump(mode="json"),
            }
        )
        return ModelArmWorkspace(
            root, transcript, tuple(paths), state_hash, prefix_hash, ledger_hash
        )
