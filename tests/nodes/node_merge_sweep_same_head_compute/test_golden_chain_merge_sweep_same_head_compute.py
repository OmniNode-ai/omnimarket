# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Same-head skip and watcher helper assertions extracted from sweep rules (OMN-20864)."""

from typing import Any

import pytest

from omnimarket.nodes.node_merge_sweep_same_head_compute import (
    HandlerMergeSweepSameHead,
)
from omnimarket.nodes.node_merge_sweep_same_head_compute.handlers.handler_merge_sweep_same_head import (
    queue_entry,
    watcher_handoff,
)
from omnimarket.nodes.node_merge_sweep_same_head_compute.models import (
    ModelMergeSweepSameHeadRequest,
)

NOW = "2026-09-29T17:00:00Z"
HEAD_A = "e7bc331c81a767026433997eed9eba6f031bf827"
HEAD_B = "3776550e2b828db3484e376f8fbf811410e127b0"
SAME_HEAD = {
    "ts": NOW,
    "from": "worker-a",
    "head": "249fbbf2c376feca592bded39027375f9fd0ffd8",
}


def chain_head(n: int = 679, **over: Any) -> dict[str, Any]:
    return {
        "repo": "project-a",
        "number": n,
        "children": [680],
        "cls": "green-unarmed",
        "state": "OPEN",
        "owner": {"state": "none"},
        "handed_off": SAME_HEAD,
        **over,
    }


def skip(head: dict[str, Any]) -> dict[str, Any] | None:
    return (
        HandlerMergeSweepSameHead()
        .handle(
            ModelMergeSweepSameHeadRequest(
                head=head,
                key=f"{head['repo']}#{head['number']}",
                scope=frozenset({"project-a"}),
            )
        )
        .skipped
    )


def test_strict_order_handoff_keeps_the_controller_reason() -> None:
    reason = "held behind strict order on project-a:main: project-a#725 goes first (cause fix: cause CLAIM lane=worker-b cause=hygiene)"
    assert skip(chain_head(strict_queued=reason)) == {
        "pr": "project-a#679",
        "why": "handed-off-same-head",
        "head": SAME_HEAD["head"],
        "reason": reason,
    }


@pytest.mark.parametrize(
    "over",
    [
        {
            "strict_queued": "held",
            "handed_off": None,
        },  # a strict listing alone still plans the lane
        {"strict_queued": None, "handed_off": None},
    ],
)
def test_chain_head_without_handoff_or_queue_is_not_skipped(
    over: dict[str, Any],
) -> None:
    h = chain_head(**over)
    assert skip(h) is None


@pytest.mark.parametrize(
    ("handed", "queued", "strict", "reason"),
    [
        (
            None,
            {"reason": "in the merge queue, position 31"},
            None,
            "in the merge queue, position 31",
        ),
        (
            SAME_HEAD,
            None,
            None,
            f"handed off to landing-controller at {SAME_HEAD['head'][:12]} at {NOW}, no head change since",
        ),
        (SAME_HEAD, {"reason": "in the merge queue"}, None, "in the merge queue"),
        (None, {"reason": "in the merge queue"}, "held", "held"),
    ],
)
def test_chain_head_queued_or_handed_off_at_its_head_is_skipped(
    handed: dict[str, Any] | None,
    queued: dict[str, Any] | None,
    strict: str | None,
    reason: str,
) -> None:
    """OMN-20864: either fact suffices, without a strict-order listing."""
    h = chain_head(head=HEAD_B, handed_off=handed, queued=queued, strict_queued=strict)
    assert skip(h) == {
        "pr": "project-a#679",
        "why": "queued-or-handed-off-same-head",
        "head": SAME_HEAD["head"] if handed else HEAD_B,
        "reason": reason,
    }


@pytest.mark.parametrize(
    "over", [{"state": "MERGED"}, {"state": "CLOSED"}, {"repo": "other-project"}]
)
def test_same_head_skip_does_not_hide_closed_or_out_of_scope_heads(
    over: dict[str, Any],
) -> None:
    h = chain_head(queued={"reason": "queued"}, strict_queued="held", **over)
    assert skip(h) is None


@pytest.mark.parametrize("queued", [True, False, None])
def test_queue_entry_uses_the_full_watcher_record_only_when_queued(
    queued: bool | None,
) -> None:
    rec = {
        "queued": queued,
        "queue": {"state": "QUEUED", "position": 31, "enqueued_at": NOW},
        "reason": "in the merge queue",
        "facts": {"state": "OPEN"},
    }
    assert queue_entry(rec) == (
        {**rec["queue"], "reason": rec["reason"]} if queued else None
    )
    assert queue_entry({"queued": True}) == {
        "state": None,
        "position": None,
        "enqueued_at": None,
        "reason": None,
    }


@pytest.mark.parametrize(
    ("head", "stored", "match"),
    [
        (HEAD_A, HEAD_A.upper(), True),
        (HEAD_B, HEAD_A, False),
        ("", "", False),
        (None, None, False),
    ],
)
def test_watcher_handoff_matches_only_a_nonempty_current_head(
    head: str | None, stored: str | None, match: bool
) -> None:
    rec = {"handoff": {"at": NOW, "lane": "worker-a", "head": stored}}
    assert watcher_handoff(rec, head) == (
        {"ts": NOW, "from": "worker-a", "head": stored} if match else None
    )
    assert watcher_handoff({}, head) is None


def test_contract_default_handler_resolves_to_the_packaged_handler() -> None:
    import importlib
    from pathlib import Path

    import yaml

    import omnimarket.nodes.node_merge_sweep_same_head_compute as package

    contract = yaml.safe_load(
        (Path(package.__file__).parent / "contract.yaml").read_text()
    )
    assert contract["name"] == "node_merge_sweep_same_head_compute"
    assert contract["event_bus"] == {"subscribe_topics": [], "publish_topics": []}
    assert contract["metadata"]["transport_type"] == "local"
    assert contract["descriptor"]["runtime_profiles"] == []
    assert contract["runtime_dispatch"]["external_trigger"].strip()
    assert "terminal_event" not in contract
    module_name, class_name = contract["handler_routing"]["default_handler"].split(":")
    handler_class = getattr(importlib.import_module(module_name), class_name)
    assert callable(getattr(handler_class(), "handle", None))
