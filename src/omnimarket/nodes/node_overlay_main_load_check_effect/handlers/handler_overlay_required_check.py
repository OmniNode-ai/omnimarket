# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Refuses a node added in a pull request whose overlay is not on overlays main (OMN-26490).

A node merged while the overlays repository held no document for it, and the dev runtime refused the node at its first tick.
Nothing ordered the overlays change before the code, so this check asks at merge time what the runtime asks at run time:
is ``<overlays root>/<overlay id>/overlay.yaml`` there?

Detection (the rule and its blind spots are stated in the pull request that adds it):

- a contract with a top-level ``overlay:`` block requires an overlay, named by ``overlay.node`` (else the directory name);
- a node directory holding ``overlay.example.yaml`` (the shape every node that reads an overlay ships) requires one named by
  the directory;
- ``overlay.required: false`` in the contract declares the overlay optional and the node is not judged.

What it reads (OMN-26490): overlays main (the bootstrap source) and, when a host names one, the host's snapshot of the settings
store: ``<root>/<overlay id>/overlay.yaml`` with its stamp, the layout ``node_overlay_store_read_effect`` writes and the runtime
reads, verified against the stamp. An overlay present in either satisfies the requirement. What it does not read: the store itself
(a broker or a database). A hosted CI runner holds no store snapshot, so there the check judges overlays main and the pull request
body only, and every refusal says the store was not read.

The same ``missing_overlays`` computes the backlog list (every node now in the tree) and the verdict on the added nodes, so
there is one implementation and two callers. The escape hatch is a merged overlays pull request named in the pull request
body that added the document, verified against the history of the overlays checkout.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from pathlib import Path

import yaml

from omnimarket.handlers.node_overlay_reader import (
    OVERLAY_FILE_NAME,
    OVERLAY_ROOTS_ENV,
    find_node_overlay,
)
from omnimarket.handlers.overlay_store.snapshot_root import (
    FileOverlaySnapshot,
    OverlaySnapshotError,
)
from omnimarket.nodes.node_overlay_main_load_check_effect.handlers.adapter_git_overlay_history import (
    NODES_DIR,
)
from omnimarket.nodes.node_overlay_main_load_check_effect.models.model_overlay_required_check import (
    ModelOverlayRequiredCommand,
    ModelOverlayRequiredFinding,
    ModelOverlayRequiredResult,
    ModelOverlayRequirement,
)

EXAMPLE_FILE_NAME = "overlay.example.yaml"
UNOWNED = "unowned"
_OVERLAYS_PR = re.compile(
    r"(?:\bOmniNode-ai/overlays#|(?<![\w/-])overlays#|github\.com/OmniNode-ai/overlays/pull/)(\d+)"
)
_TICKET = re.compile(r"\bOMN-\d+\b")


class OverlayContractUnreadableError(ValueError):
    """A node's contract cannot be read, so what it requires cannot be judged."""


def overlay_requirement(node_dir: Path) -> ModelOverlayRequirement | None:
    """What overlay the node requires, or ``None`` when nothing in the directory shows it reads one."""
    block: dict[object, object] | None = None
    contract = node_dir / "contract.yaml"
    if contract.is_file():
        try:
            raw = yaml.safe_load(contract.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, yaml.YAMLError) as exc:
            raise OverlayContractUnreadableError(
                f"{contract} is unreadable ({type(exc).__name__})"
            ) from None
        if isinstance(raw, dict) and isinstance(raw.get("overlay"), dict):
            block = raw["overlay"]
    has_example = (node_dir / EXAMPLE_FILE_NAME).is_file()
    if block is None and not has_example:
        return None
    named = (block or {}).get("node")
    return ModelOverlayRequirement(
        node=node_dir.name,
        overlay_id=named if isinstance(named, str) and named else node_dir.name,
        required=(block or {}).get("required") is not False,
        source="contract_block" if block is not None else "example_file",
    )


def overlay_path(overlays_root: Path, overlay_id: str) -> Path:
    return overlays_root / overlay_id / OVERLAY_FILE_NAME


def _overlay_present(overlays_root: Path, overlay_id: str) -> bool:
    return (
        find_node_overlay(overlay_id, {OVERLAY_ROOTS_ENV: str(overlays_root)})
        is not None
    )


def _store_holds(store_snapshot_root: Path | None, overlay_id: str) -> bool:
    """True when the store snapshot holds the overlay and the file matches its stamp; an unnamed snapshot holds nothing."""
    if store_snapshot_root is None:
        return False
    try:
        FileOverlaySnapshot(store_snapshot_root).read_stamp(overlay_id)
    except OverlaySnapshotError:
        return False
    return True


def store_reading(store_snapshot_root: str | None) -> str:
    """What the verdict says of the settings store: which snapshot was read, or that none was."""
    if store_snapshot_root is None:
        return (
            "the settings store was not read (no snapshot root was named, as on a hosted runner), so only overlays main "
            "and the pull request body were judged"
        )
    return f"the settings store snapshot at {store_snapshot_root} was read and does not hold it"


def missing_overlays(
    repo_root: Path,
    overlays_root: Path,
    nodes: tuple[str, ...] | None = None,
    store_snapshot_root: Path | None = None,
) -> tuple[ModelOverlayRequirement, ...]:
    """The nodes (all of them, or ``nodes``) that require an overlay neither overlays main nor the store snapshot holds."""
    nodes_dir = repo_root / NODES_DIR
    names = (
        nodes
        if nodes is not None
        else tuple(
            sorted(
                p.name
                for p in nodes_dir.iterdir()
                if p.is_dir() and p.name.startswith("node_")
            )
        )
    )
    missing: list[ModelOverlayRequirement] = []
    for name in names:
        requirement = overlay_requirement(nodes_dir / name)
        if (
            requirement is not None
            and requirement.required
            and not _overlay_present(overlays_root, requirement.overlay_id)
            and not _store_holds(store_snapshot_root, requirement.overlay_id)
        ):
            missing.append(requirement)
    return tuple(missing)


def named_overlay_prs(body: str) -> tuple[int, ...]:
    """The overlays pull request numbers a body names: ``overlays#n``, ``OmniNode-ai/overlays#n`` or the pull request URL."""
    return tuple(int(n) for n in _OVERLAYS_PR.findall(body))


def owner_of(node_dir: Path, first_commit_subject: Callable[[str], str]) -> str:
    """The contract's ``owner``, else ``unowned`` with the ticket in the subject of the commit that added the node."""
    contract = node_dir / "contract.yaml"
    try:
        raw = yaml.safe_load(contract.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError):
        raw = None
    owner = raw.get("owner") if isinstance(raw, dict) else None
    if isinstance(owner, str) and owner.strip():
        return owner.strip()
    match = _TICKET.search(first_commit_subject(node_dir.name))
    return f"{UNOWNED} ({match.group(0)})" if match else UNOWNED


class HandlerOverlayRequiredCheck:
    """Judges the nodes a pull request adds against the overlays tree."""

    def __init__(self, *, merged_pr_adds: Callable[[Path, int, str], bool]) -> None:
        """``merged_pr_adds(checkout, pr, overlay_id)`` says whether a merged overlays pull request added the document."""
        self._merged_pr_adds = merged_pr_adds

    def _named_pr_adds(
        self, command: ModelOverlayRequiredCommand, overlay_id: str
    ) -> bool:
        if command.overlays_checkout is None:
            return False
        checkout = Path(command.overlays_checkout)
        return any(
            self._merged_pr_adds(checkout, pr, overlay_id)
            for pr in named_overlay_prs(command.pr_body)
        )

    def handle(
        self, command: ModelOverlayRequiredCommand
    ) -> ModelOverlayRequiredResult:
        overlays_root = Path(command.overlays_root)
        findings: list[ModelOverlayRequiredFinding] = []
        try:
            missing = missing_overlays(
                Path(command.repo_root),
                overlays_root,
                command.nodes,
                Path(command.store_snapshot_root)
                if command.store_snapshot_root
                else None,
            )
        except OverlayContractUnreadableError as exc:
            findings.append(
                ModelOverlayRequiredFinding(
                    node="contract",
                    overlay_id="",
                    looked_for="",
                    detail=f"{exc}: what the node requires cannot be judged",
                )
            )
            missing = ()
        for requirement in missing:
            if self._named_pr_adds(command, requirement.overlay_id):
                continue
            looked_for = overlay_path(overlays_root, requirement.overlay_id)
            findings.append(
                ModelOverlayRequiredFinding(
                    node=requirement.node,
                    overlay_id=requirement.overlay_id,
                    looked_for=str(looked_for),
                    detail=(
                        f"{requirement.node} requires the overlay {requirement.overlay_id} (detected from its "
                        f"{requirement.source}) and overlays main has no {looked_for}; "
                        f"{store_reading(command.store_snapshot_root)}. Either merge the overlay "
                        f"first, or name the merged overlays pull request that adds it in the pull request body "
                        f"(a merged overlays pull request, as overlays#<number>)."
                    ),
                )
            )
        return ModelOverlayRequiredResult(
            ok=not findings, judged=command.nodes, findings=tuple(findings)
        )
