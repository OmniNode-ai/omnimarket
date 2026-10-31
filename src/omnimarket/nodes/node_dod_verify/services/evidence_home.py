# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Where node_dod_verify may write a DoD receipt and its retry record (OMN-21776).

The command line writes ``dod_report.json`` and ``dod_retry_state.json`` under
``ONEX_EVIDENCE_ROOT`` (or at ``--output-path``). A host whose environment still
names a directory inside a retired repository would recreate that directory on
every run, so the workspace reconcile could never retire it. The retired
directory names and the home beneath the runtime state root both come from the
runtime-path authority (``configs/runtime_paths.yaml``, ``dod_evidence``); this
module holds no path literal.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from omnimarket.config.state_root import (
    ONEX_STATE_ROOT_ENV_KEYS,
    OnexStateRootUnconfiguredError,
    resolve_onex_state_root,
)
from omnimarket.handlers.runtime_paths import runtime_path, runtime_path_list
from omnimarket.protocols.protocol_dod_verify_retry_ledger import (
    FilesystemDodVerifyRetryLedger,
)


class EvidenceRootRetiredError(ValueError):
    """The receipt root or output path sits inside a retired repository's directory."""


def declared_evidence_home() -> str:
    """Return the evidence home: ``<state root>/<home_subdir>``, the state root named when unset."""
    subdir = runtime_path("dod_evidence.home_subdir")
    try:
        return str(resolve_onex_state_root(purpose="the DoD evidence home") / subdir)
    except OnexStateRootUnconfiguredError:
        return f"<{' or '.join(ONEX_STATE_ROOT_ENV_KEYS)}>/{subdir}"


def refuse_retired_path(path: Path, *, source: str) -> None:
    """Raise ``EvidenceRootRetiredError`` when ``path`` has a retired root as a component.

    The path as written and its symlink-resolved form are both checked, so a link
    into a retired directory is refused the same as the directory itself.
    """
    retired = frozenset(runtime_path_list("dod_evidence.retired_roots"))
    expanded = path.expanduser()
    for candidate in (expanded, expanded.resolve(strict=False)):
        hit = retired.intersection(candidate.parts)
        if hit:
            raise EvidenceRootRetiredError(
                f"{source} {path} is inside the retired repository directory "
                f"{sorted(hit)[0]}; nothing was written. Set ONEX_EVIDENCE_ROOT to "
                f"{declared_evidence_home()}."
            )


def open_receipt_destinations(
    output_path: Path | None,
) -> tuple[str | None, FilesystemDodVerifyRetryLedger | None]:
    """Return ``(ONEX_EVIDENCE_ROOT, retry ledger)`` after refusing a retired destination.

    A refused ``ONEX_EVIDENCE_ROOT`` or ``--output-path`` is reported on stderr and exits 2 before
    the retry record, or anything else, is written there. With the root unset the node is in its
    documented stdout-only mode (OMN-17022): there is nowhere durable to record an attempt and no
    fallback path is invented, so the ledger is ``None``.
    """
    root = os.environ.get("ONEX_EVIDENCE_ROOT")
    try:
        if root:
            refuse_retired_path(Path(root), source="ONEX_EVIDENCE_ROOT")
        if output_path is not None:
            refuse_retired_path(output_path, source="--output-path")
    except EvidenceRootRetiredError as exc:
        sys.stderr.write(f"{exc}\n")
        sys.exit(2)
    ledger = (
        FilesystemDodVerifyRetryLedger(ticket_state_root=Path(root)) if root else None
    )
    return root, ledger


__all__ = [
    "EvidenceRootRetiredError",
    "declared_evidence_home",
    "open_receipt_destinations",
    "refuse_retired_path",
]
