# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Reject ``[skip-*]`` bypass tokens (operating rule 10).

Pure scan of text a caller already read. A text carrying ``[skip-<letter>`` (any case) is a
violation unless the same text also carries ``# skip-token-allowed: <receipt-id>``, the one
sanctioned escape hatch; free-text justification never passes. A staged file is in scope only by
its type: PR-body-like files (``.md``, ``.yaml``, ``.yml``, ``.txt``) and committed session
evidence (``.onex_state/evidence/`` files ending ``.err``, ``.json``, ``.jsonl``, ``.log``,
``.out``), so source that discusses the token is not blocked. A commit message and a PR body are
always in scope.

This is the logic of the vendored ``reject-deploy-gate-skip-token.sh`` (OMN-10414, OMN-12696), which
the remote pre-commit export still runs as its transport; the tests hold the two to the same verdicts.
"""

from __future__ import annotations

import re
from typing import Final

from omnimarket.nodes.node_reject_skip_token_compute.models.model_skip_token_scan import (
    EnumSkipTokenSurface,
    EnumSkipTokenVerdict,
    ModelSkipTokenFinding,
    ModelSkipTokenScanItem,
    ModelSkipTokenScanRequest,
    ModelSkipTokenScanResult,
)

# Line-local like grep: ``[ \t\r\f\v]`` is POSIX ``[[:space:]]`` without the newline, so an approval
# receipt on the line after the marker is not an approval.
SKIP_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"\[skip-[a-zA-Z]", re.IGNORECASE | re.ASCII
)
ALLOWLIST_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"#[ \t\r\f\v]*skip-token-allowed:[ \t\r\f\v]*[^ \t\r\n\f\v]",
    re.IGNORECASE | re.ASCII,
)

_PROSE_SUFFIXES: Final[tuple[str, ...]] = (".md", ".yaml", ".yml", ".txt")
_EVIDENCE_SUFFIXES: Final[tuple[str, ...]] = (".err", ".json", ".jsonl", ".log", ".out")
_EVIDENCE_DIR: Final[str] = ".onex_state/evidence/"


def in_scope(item: ModelSkipTokenScanItem) -> bool:
    """A message or a PR body is always scanned; a staged file by its type (suffixes are case-sensitive)."""
    if item.surface is not EnumSkipTokenSurface.STAGED_FILE:
        return True
    path = item.path
    if path.endswith(_PROSE_SUFFIXES):
        return True
    in_evidence = path.startswith(_EVIDENCE_DIR) or f"/{_EVIDENCE_DIR}" in path
    return in_evidence and path.endswith(_EVIDENCE_SUFFIXES)


class HandlerRejectSkipToken:
    """definition-B: ``handle(ModelSkipTokenScanRequest) -> ModelSkipTokenScanResult``."""

    def handle(self, request: ModelSkipTokenScanRequest) -> ModelSkipTokenScanResult:
        findings: list[ModelSkipTokenFinding] = []
        out_of_scope: list[str] = []
        scanned = 0
        for item in request.items:
            if not in_scope(item):
                out_of_scope.append(item.path)
                continue
            scanned += 1
            if SKIP_PATTERN.search(item.text) is None:
                continue
            findings.append(
                ModelSkipTokenFinding(
                    surface=item.surface,
                    path=item.path,
                    allowed=ALLOWLIST_PATTERN.search(item.text) is not None,
                )
            )
        blocked = any(not f.allowed for f in findings)
        return ModelSkipTokenScanResult(
            verdict=EnumSkipTokenVerdict.BLOCK
            if blocked
            else EnumSkipTokenVerdict.PASS,
            scanned=scanned,
            out_of_scope=tuple(out_of_scope),
            findings=tuple(findings),
        )
