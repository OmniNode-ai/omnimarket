# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The send gate of one lab-fill lane: what is read about the ticket and the host right before the lane is sent (OMN-23277).

Two defects of the old lab-fill.js send step are closed here. It sent a lane for a ticket whose implementation
pull request had merged (about 45 fill lanes on 2026-11-09 found the work merged and ended done or skipped-owned),
and it sent a brief whose facts live on a fact host to a host that cannot reach it.
"""

from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum
from importlib import resources

import yaml
from pydantic import BaseModel, ConfigDict, Field

_FROZEN = ConfigDict(frozen=True, extra="forbid")

# A lane of these kinds works on a pull request, or is the closer's own lane, so a merged implementation does not
# stop it: pr-red, pr-stalled and pr-land lanes act on one named pull request; dod-verify IS the closing lane; diagnose
# belongs to the fill itself.
CLOSER_EXEMPT_KINDS: tuple[str, ...] = (
    "pr-red",
    "pr-stalled",
    "pr-land",
    "dod-verify",
    "diagnose",
)


def _load_fact_hosts() -> tuple[str, ...]:
    """The hosts whose facts a brief may cite and that only some lab hosts reach, read from ``send_gate`` in contract.yaml."""
    text = (
        resources.files("omnimarket.nodes.node_lab_fill_effect")
        .joinpath("contract.yaml")
        .read_text()
    )
    block = (yaml.safe_load(text) or {}).get("send_gate") or {}
    return tuple(str(h) for h in block.get("fact_hosts", ()))


DEFAULT_FACT_HOSTS: tuple[str, ...] = _load_fact_hosts()
CLOSER_NODE = "node_dod_closeout_sweep_compute"


class EnumLabFillSendVerdict(StrEnum):
    """The gate's one answer for a lane."""

    SEND = "send"
    IMPLEMENTATION_MERGED = "implementation-merged"
    FACT_HOST_UNREACHABLE = "fact-host-unreachable"


class ModelLabFillSendGateRequest(BaseModel):
    """The facts the gate judges from; the caller reads them, the gate reads nothing."""

    model_config = _FROZEN

    ticket: str = Field(pattern=r"^OMN-[0-9]+$")
    kind: str
    brief: str
    # The host the lane is pinned to, or None when the runner places it.
    host: str | None = None
    # Merged pull requests that name the ticket (repo#number), companion repositories already left out.
    merged_prs: tuple[str, ...] = ()
    exempt_kinds: tuple[str, ...] = CLOSER_EXEMPT_KINDS
    # Hosts whose facts the brief may cite; a brief naming one needs a host that reaches it.
    fact_hosts: tuple[str, ...] = DEFAULT_FACT_HOSTS
    # Hosts the caller already knows the facts need, beside the ones read from the brief's own words.
    needs_hosts: tuple[str, ...] = ()
    # Host name -> hosts it reaches. A host always reaches itself; a host absent here reaches no other (fail closed).
    host_reach: Mapping[str, tuple[str, ...]] = Field(default_factory=dict)
    closer: str = CLOSER_NODE


class ModelLabFillSendGateResult(BaseModel):
    """Send, or why the lane is not sent and who owns the ticket instead."""

    model_config = _FROZEN

    verdict: EnumLabFillSendVerdict
    detail: str = ""
    # The host the lane must be sent to when the gate chose one for an unpinned fact-host-bound brief.
    host: str | None = None
    # The node that owns a merged ticket's close-out; set only for implementation-merged.
    routed_to: str = ""
    merged_prs: tuple[str, ...] = ()
    needs_hosts: tuple[str, ...] = ()
