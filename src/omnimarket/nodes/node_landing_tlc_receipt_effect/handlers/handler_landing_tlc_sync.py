# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Install published TLC receipts where the controller's installer looks for them.

``refresh_install`` reads ``<landing state>/g-act-<pin[:8]>/tlc.json`` and refuses
TLC-RECEIPT-MISSING without one. Each release this repository holds for a pin the state directory
lacks (or holds for another model) is downloaded, put through the installed controller's own gate
again, and only then moved into place, whole or not at all.
"""

from __future__ import annotations

import json
import re
import subprocess
import tarfile
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Literal

from ..models import ModelLandingTlcSyncRequest, ModelLandingTlcSyncResult
from .handler_landing_tlc_gate import (
    PIN_DIR_PREFIX,
    TAG_PREFIX,
    check_tlc_receipt,
    load_gate,
)

TAG = re.compile(
    rf"^{re.escape(TAG_PREFIX)}(?P<pin8>[0-9a-f]{{8}})-(?P<model8>[0-9a-f]{{8}})$"
)
RELEASE_LIST_LIMIT = 30
GH_TIMEOUT_S = 120

Runner = Callable[[list[str]], subprocess.CompletedProcess[str]]


def _run(argv: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        argv, capture_output=True, text=True, timeout=GH_TIMEOUT_S, check=False
    )


def _local_model8(directory: Path) -> str:
    try:
        return str(json.loads((directory / "tlc.json").read_text())["model_sha256"])[:8]
    except (OSError, ValueError, KeyError, TypeError):
        return ""


class HandlerLandingTlcSync:
    """Pull, re-verify, then move into place."""

    def __init__(self, run: Runner = _run) -> None:
        self._run = run

    def _newest_per_pin(
        self, repo: str, keep: int
    ) -> tuple[list[tuple[str, str, str]], str]:
        listed = self._run(
            [
                "gh",
                "release",
                "list",
                "--repo",
                repo,
                "--limit",
                str(RELEASE_LIST_LIMIT),
                "--json",
                "tagName",
            ]
        )
        if listed.returncode:
            return [], f"gh release list failed: {listed.stderr.strip()}"
        try:
            tags = [row["tagName"] for row in json.loads(listed.stdout)]
        except (ValueError, KeyError, TypeError) as exc:
            return [], f"gh release list returned unreadable output: {exc}"
        seen: dict[str, tuple[str, str, str]] = {}
        for tag in tags:
            match = TAG.match(tag)
            if match and match["pin8"] not in seen:
                seen[match["pin8"]] = (tag, match["pin8"], match["model8"])
        return list(seen.values())[:keep], ""

    def _install(
        self, request: ModelLandingTlcSyncRequest, gate_tag: tuple[str, str, str]
    ) -> str:
        """The refusal reason, or an empty string once the receipt is in place."""
        tag, pin8, model8 = gate_tag
        state = request.state_dir
        with tempfile.TemporaryDirectory(dir=state, prefix=".landing-tlc-") as scratch:
            work = Path(scratch)
            downloaded = self._run(
                [
                    "gh",
                    "release",
                    "download",
                    tag,
                    "--repo",
                    request.repo,
                    "--pattern",
                    f"{PIN_DIR_PREFIX}*.tar.gz",
                    "--dir",
                    str(work),
                ]
            )
            if downloaded.returncode:
                return f"{tag}: gh release download failed: {downloaded.stderr.strip()}"
            archives = sorted(work.glob(f"{PIN_DIR_PREFIX}{pin8}.tar.gz"))
            if len(archives) != 1:
                return f"{tag}: want one {PIN_DIR_PREFIX}{pin8}.tar.gz asset, found {len(archives)}"
            extract = work / "extract"
            extract.mkdir()
            try:
                with tarfile.open(archives[0]) as packed:
                    packed.extractall(extract, filter="data")
            except (tarfile.TarError, OSError) as exc:
                return f"{tag}: the archive is refused: {exc}"
            directory = extract / f"{PIN_DIR_PREFIX}{pin8}"
            if not directory.is_dir() or [p.name for p in extract.iterdir()] != [
                directory.name
            ]:
                return f"{tag}: the archive must hold exactly {directory.name}"
            try:
                pin = str(json.loads((directory / "tlc.json").read_text())["t3_commit"])
                gate = load_gate(request.gate_script)
            except (
                OSError,
                ValueError,
                KeyError,
                TypeError,
                ImportError,
                SyntaxError,
            ) as exc:
                return f"{tag}: cannot read the receipt or the gate: {exc}"
            if pin[:8] != pin8:
                return f"{tag}: the receipt is bound to {pin[:8]}, not {pin8}"
            reasons = check_tlc_receipt(gate, directory, pin)
            if reasons:
                return f"{tag}: the gate refuses the receipt: " + "; ".join(reasons)
            if _local_model8(directory) != model8:
                return f"{tag}: the receipt's model is not the one the release names"
            destination = state / directory.name
            if destination.exists():
                destination.rename(work / "replaced")
            directory.rename(destination)
        return ""

    def handle(self, request: ModelLandingTlcSyncRequest) -> ModelLandingTlcSyncResult:
        request.state_dir.mkdir(parents=True, exist_ok=True)
        wanted, failure = self._newest_per_pin(request.repo, request.keep_newest)
        if failure:
            return ModelLandingTlcSyncResult(status="failed", reasons=(failure,))
        installed: list[str] = []
        current: list[str] = []
        reasons: list[str] = []
        for entry in wanted:
            tag, pin8, model8 = entry
            if _local_model8(request.state_dir / f"{PIN_DIR_PREFIX}{pin8}") == model8:
                current.append(tag)
                continue
            reason = self._install(request, entry)
            if reason:
                reasons.append(reason)
            else:
                installed.append(tag)
        status: Literal["synced", "current", "refused", "failed"]
        if installed:
            status = "synced"
        elif reasons:
            status = "refused"
        else:
            status = "current"
        return ModelLandingTlcSyncResult(
            status=status,
            installed=tuple(installed),
            current=tuple(current),
            reasons=tuple(reasons),
        )
