# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-23895: which host-held labels are adopted into the store, and the failing controls."""

from __future__ import annotations

import pytest

from omnimarket.nodes.node_claude_pool_store_adopt_compute.handlers.handler_store_adopt import (
    HandlerStoreAdopt,
)
from omnimarket.nodes.node_claude_pool_store_adopt_compute.models import (
    ModelHeldLabel,
    ModelStoreAdoptPolicy,
    ModelStoreAdoptRequest,
    ModelStoreToken,
)

pytestmark = pytest.mark.unit

POLICY = ModelStoreAdoptPolicy(key_prefix="CLAUDE_CODE_OAUTH_")


def held(
    host: str, label: str, fingerprint: str, revoked: bool = False
) -> ModelHeldLabel:
    return ModelHeldLabel(
        host=host, label=label, fingerprint=fingerprint, revoked=revoked
    )


def decide(
    held_rows: tuple[ModelHeldLabel, ...],
    store: tuple[ModelStoreToken, ...] = (),
    policy: ModelStoreAdoptPolicy | None = POLICY,
):  # type: ignore[no-untyped-def]
    return HandlerStoreAdopt().handle(
        ModelStoreAdoptRequest(store_tokens=store, held=held_rows, policy=policy)
    )


def outcome(result, label: str) -> str:  # type: ignore[no-untyped-def]
    return next(d.outcome for d in result.decisions if d.label == label)


def test_a_live_label_outside_the_store_is_adopted_from_the_first_live_host() -> None:
    result = decide(
        (held("h201", "onexbot", "4d2430e0"), held("h053", "onexbot", "4d2430e0"))
    )
    assert outcome(result, "onexbot") == "adopt"
    (command,) = result.commands
    assert (command.label, command.source_host, command.fingerprint) == (
        "onexbot",
        "h053",
        "4d2430e0",
    )
    assert command.key == "CLAUDE_CODE_OAUTH_ONEXBOT"


def test_a_hyphenated_label_makes_an_underscored_key() -> None:
    (command,) = decide((held("h053", "h101-own", "aaaaaaaa"),)).commands
    assert command.key == "CLAUDE_CODE_OAUTH_H101_OWN"


def test_the_overlays_own_key_wins_over_the_derived_one() -> None:
    store = (
        ModelStoreToken(
            label="onexbot", key="CLAUDE_CODE_OAUTH_CUSTOM", fingerprint=None
        ),
    )
    (command,) = decide((held("h053", "onexbot", "4d2430e0"),), store).commands
    assert command.key == "CLAUDE_CODE_OAUTH_CUSTOM"


def test_a_label_the_store_already_holds_is_left_alone() -> None:
    store = (ModelStoreToken(label="caia", key="K", fingerprint="c62464b2"),)
    result = decide((held("h053", "caia", "c62464b2"),), store)
    assert outcome(result, "caia") == "in_store"
    assert result.commands == ()


def test_control_a_store_value_that_differs_is_never_overwritten() -> None:
    store = (ModelStoreToken(label="caia", key="K", fingerprint="11111111"),)
    result = decide((held("h053", "caia", "c62464b2"),), store)
    assert outcome(result, "caia") == "store_differs"
    assert result.commands == ()


def test_control_hosts_that_disagree_adopt_nothing() -> None:
    result = decide((held("h053", "x", "aaaaaaaa"), held("h201", "x", "bbbbbbbb")))
    assert outcome(result, "x") == "conflicting_fingerprints"
    assert result.commands == ()


def test_control_a_label_revoked_everywhere_is_not_adopted() -> None:
    result = decide(
        (
            held("h053", "acct-1019b", "cccccccc", True),
            held("h201", "acct-1019b", "cccccccc", True),
        )
    )
    assert outcome(result, "acct-1019b") == "revoked_everywhere"
    assert result.commands == ()


def test_a_label_revoked_on_one_host_is_adopted_from_the_live_one() -> None:
    result = decide(
        (held("h053", "z", "dddddddd", True), held("h201", "z", "dddddddd"))
    )
    (command,) = result.commands
    assert command.source_host == "h201"


def test_without_the_overlay_block_nothing_is_decided() -> None:
    result = decide((held("h053", "onexbot", "4d2430e0"),), policy=None)
    assert result.configured is False
    assert result.decisions == ()
