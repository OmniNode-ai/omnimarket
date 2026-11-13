# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Error chain: red and unread tags, no home under the hooks, the closed request and the failing control (OMN-22641)."""

from __future__ import annotations

import re

import pytest
from pydantic import ValidationError

from omnimarket.nodes.node_sibling_pin_hook_revs_compute.handlers import (
    HandlerSiblingPinHookRevs,
)
from omnimarket.nodes.node_sibling_pin_hook_revs_compute.models import (
    EnumHookRevsReason,
    ModelHookRevsCandidate,
    ModelSiblingPinHookRevsPlan,
    ModelSiblingPinHookRevsRequest,
)

PIN = "dev-20261105190003-87a6f5ddcbcc"
HOOK = "dev-20261105194015-1adbf46062bd"
MID = "dev-20261106041752-8377609527be"
NEWEST = "dev-20261106051752-0123456789ab"
OLD_HOOK = "dev-20261018024445-b6ee996befa1"
STAMP = re.compile(r"^dev-(?P<stamp>[0-9]{14})-[0-9a-f]{12}$")


def stamp(tag: str) -> str:
    match = STAMP.fullmatch(tag)
    assert match is not None
    return match.group("stamp")


def plan(
    pin: str,
    hooks: tuple[str, ...],
    tags: tuple[str, ...],
    red: tuple[str, ...] = (),
    unread: tuple[str, ...] = (),
) -> ModelSiblingPinHookRevsPlan:
    candidates = tuple(
        ModelHookRevsCandidate(tag=t, green=None if t in unread else t not in red)
        for t in tags
    )
    return HandlerSiblingPinHookRevs().handle(
        ModelSiblingPinHookRevsRequest(
            pin_tag=pin, hook_revs=hooks, candidates=candidates
        )
    )


def written(
    pin: str, hooks: tuple[str, ...], result: ModelSiblingPinHookRevsPlan
) -> tuple[str, tuple[str, ...]]:
    """The pin and the hook revs the train writes from a plan."""
    new_hooks = tuple(
        result.hook_tag if index in result.hook_indexes and result.hook_tag else rev
        for index, rev in enumerate(hooks)
    )
    return result.pin_tag or pin, new_hooks


def old_train_plan(
    pin: str, hooks: tuple[str, ...], tags: tuple[str, ...]
) -> ModelSiblingPinHookRevsPlan:
    """The train before this node: the pin takes the newest green tag, no hook block moves (omniclaude#3188)."""
    newer = [t for t in tags if stamp(t) > stamp(pin)]
    return ModelSiblingPinHookRevsPlan(
        hook_tag=None,
        hook_indexes=(),
        pin_tag=newer[0] if newer else None,
        reason=EnumHookRevsReason.PIN_MOVED_NO_BOUND_HOOKS,
    )


def passes_both_rules(pin: str, hooks: tuple[str, ...]) -> bool:
    """The pin-floor tests (stamp not older) and first-party-ref (one home per literal), on the floor-bound blocks."""
    bound = [h for h in hooks if stamp(h) >= stamp(PIN)]
    return all(stamp(h) > stamp(pin) and h != pin for h in bound)


def test_failing_control_the_old_train_writes_a_tree_that_fails_both_rules() -> None:
    hooks = (HOOK, OLD_HOOK, HOOK)
    tags = (NEWEST, MID, HOOK, PIN)
    old_pin, old_hooks = written(PIN, hooks, old_train_plan(PIN, hooks, tags))
    assert old_pin == NEWEST
    assert not passes_both_rules(old_pin, old_hooks)
    new_pin, new_hooks = written(PIN, hooks, plan(PIN, hooks, tags))
    assert passes_both_rules(new_pin, new_hooks)


def test_a_red_newest_tag_is_passed_over_for_both_homes() -> None:
    result = plan(PIN, (HOOK,), (NEWEST, MID, HOOK, PIN), red=(NEWEST,))
    assert (result.hook_tag, result.pin_tag) == (MID, HOOK)
    assert passes_both_rules(*written(PIN, (HOOK,), result))


def test_an_unread_tag_is_never_chosen() -> None:
    result = plan(PIN, (HOOK,), (NEWEST, MID, HOOK, PIN), unread=(NEWEST, MID))
    assert result.hook_tag is None
    assert result.pin_tag is None
    assert result.reason is EnumHookRevsReason.NO_GREEN_TAG_UNDER_HOOKS


def test_hooks_already_ahead_leave_the_pin_to_move_alone() -> None:
    result = plan(PIN, (MID,), (MID, HOOK, PIN))
    assert (result.hook_tag, result.hook_indexes) == (None, ())
    assert result.pin_tag == HOOK
    assert result.reason is EnumHookRevsReason.PIN_MOVED_HOOKS_ALREADY_AHEAD


def test_no_green_tag_under_the_hooks_leaves_the_pin_where_it_was() -> None:
    result = plan(PIN, (PIN,), (MID, PIN))
    assert result.hook_tag == MID
    assert result.hook_indexes == (0,)
    assert result.pin_tag is None
    assert result.reason is EnumHookRevsReason.NO_GREEN_TAG_UNDER_HOOKS


def test_no_newer_tag_plans_nothing() -> None:
    result = plan(PIN, (HOOK,), (PIN,))
    assert (result.hook_tag, result.hook_indexes, result.pin_tag) == (None, (), None)
    assert result.reason is EnumHookRevsReason.NO_NEWER_GREEN_TAG


def test_a_hook_block_older_than_the_pin_is_not_bound_by_it() -> None:
    result = plan(PIN, (OLD_HOOK,), (MID, HOOK, PIN))
    assert result.hook_indexes == ()
    assert result.pin_tag == MID


def test_a_rev_that_is_not_a_dev_merge_tag_is_ignored() -> None:
    result = plan(PIN, ("v0.47.46", HOOK), (NEWEST, MID, HOOK, PIN))
    assert result.hook_indexes == (1,)


def test_the_request_is_closed() -> None:
    with pytest.raises(ValidationError):
        ModelSiblingPinHookRevsRequest.model_validate(
            {"pin_tag": PIN, "hook_revs": (), "candidates": (), "surprise": 1}
        )
    with pytest.raises(ValidationError):
        ModelHookRevsCandidate.model_validate({"tag": PIN, "green": True, "x": 1})


def test_every_small_night_writes_a_tree_that_passes_both_rules() -> None:
    """Over every placement of hook blocks and red tags on a five-tag ladder, a written tree holds both rules."""
    ladder = (
        "dev-20261106051752-0123456789ab",
        "dev-20261106041752-8377609527be",
        "dev-20261105194015-1adbf46062bd",
        "dev-20261105190003-87a6f5ddcbcc",
    )
    pin = ladder[-1]
    checked = 0
    for mask in range(2 ** len(ladder)):
        red = tuple(t for i, t in enumerate(ladder) if mask >> i & 1)
        for hooks in [(h,) for h in ladder] + [(ladder[2], OLD_HOOK, ladder[0])]:
            result = plan(pin, hooks, ladder, red=red)
            new_pin, new_hooks = written(pin, hooks, result)
            bound = [h for h in new_hooks if stamp(h) >= stamp(pin)]
            if result.pin_tag is not None:
                assert all(stamp(h) > stamp(new_pin) and h != new_pin for h in bound), (
                    hooks,
                    red,
                    result,
                )
            checked += 1
    assert checked == 16 * 5
