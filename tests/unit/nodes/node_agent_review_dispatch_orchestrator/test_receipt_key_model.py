# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The model of the receipt key ``repo#pr#head``, written and enumerated before the handler.

The one place requests contend is the insert-if-absent of a receipt under that key. The
model below is the request path reduced to its steps on that key, each step atomic:

    read the key; if empty, run the lab review, sign a candidate, insert-if-absent,
    take the value the key holds; post that value; record the terminal.

It is enumerated, not sampled. The key starts ``absent`` or ``stored``. Two requests, each
of which may crash at one of four points (before the lab review, after signing and before
storing, after storing and before the post, after the post) or not at all, run under every
interleaving of their steps; each crashed request is then retried alone. After every one of
those runs: the key holds exactly one receipt that was written once, every review posted
carries the stored id, one receipt is stored and at least one review is posted, and a key
that started ``stored`` caused no lab review. The mutants (an insert that overwrites, a
post of the request's own candidate) are caught by the same check, so it is not vacuous.
"""

from __future__ import annotations

import itertools
from collections.abc import Callable, Generator
from dataclasses import dataclass, field

import pytest

pytestmark = pytest.mark.unit

KEY = "OmniNode-ai/example#7#" + "a" * 40
CRASH_POINTS: tuple[str | None, ...] = (
    None,
    "before_lab_review",
    "after_sign_before_store",
    "after_store_before_post",
    "after_post",
)
STEPS_PER_REQUEST = 7


@dataclass
class World:
    """The shared state the requests contend over."""

    stored: str | None = None
    store_writes: int = 0
    lab_reviews: int = 0
    posts: list[str] = field(default_factory=list)
    nonces: int = 0
    overwrite_on_insert: bool = False
    post_own_candidate: bool = False

    def nonce(self) -> str:
        self.nonces += 1
        return f"receipt-{self.nonces}"


@dataclass
class Run:
    crashed: bool = False


def request(
    world: World, run: Run, crash_at: str | None
) -> Generator[None, None, None]:
    """One request; each ``yield`` is a point where the other request may run."""
    held = world.stored
    yield
    candidate: str | None = None
    if held is None:
        if crash_at == "before_lab_review":
            run.crashed = True
            return
        world.lab_reviews += 1
        yield
        candidate = world.nonce()
        yield
        if crash_at == "after_sign_before_store":
            run.crashed = True
            return
        if world.stored is None or world.overwrite_on_insert:
            world.stored = candidate
            world.store_writes += 1
        held = world.stored
        yield
        if crash_at == "after_store_before_post":
            run.crashed = True
            return
    yield
    world.posts.append(
        candidate if world.post_own_candidate and candidate else str(held)
    )
    yield
    if crash_at == "after_post":
        run.crashed = True
        return
    yield


def run_schedule(
    world: World, crashes: tuple[str | None, str | None], schedule: tuple[int, ...]
) -> list[Run]:
    runs = [Run(), Run()]
    gens = [request(world, runs[i], crashes[i]) for i in range(2)]
    live = [True, True]
    for pick in schedule:
        if live[pick]:
            try:
                next(gens[pick])
            except StopIteration:
                live[pick] = False
    for pick in (0, 1):
        while live[pick]:
            try:
                next(gens[pick])
            except StopIteration:
                live[pick] = False
    for run in runs:
        if run.crashed:
            retry = Run()
            for _ in request(world, retry, None):
                pass
    return runs


def violations(world: World, started_stored: bool) -> list[str]:
    found: list[str] = []
    if world.store_writes > (0 if started_stored else 1):
        found.append(f"{world.store_writes} receipts were written to one key")
    if world.stored is None:
        found.append("no receipt is stored after the retry")
    for posted in world.posts:
        if posted != world.stored:
            found.append(f"posted {posted}, the key holds {world.stored}")
    if not world.posts:
        found.append("nothing is posted after the retry")
    if started_stored and world.lab_reviews:
        found.append("a key that started stored caused a lab review")
    return found


def schedules() -> list[tuple[int, ...]]:
    """Every interleaving of two requests of ``STEPS_PER_REQUEST`` steps each."""
    out: list[tuple[int, ...]] = []
    for ones in itertools.combinations(range(2 * STEPS_PER_REQUEST), STEPS_PER_REQUEST):
        slots = [0] * (2 * STEPS_PER_REQUEST)
        for index in ones:
            slots[index] = 1
        out.append(tuple(slots))
    return out


def enumerate_all(configure: Callable[[World], None]) -> tuple[int, list[str]]:
    """Run every (initial key state, crash pair, interleaving); return the run count and violations."""
    count = 0
    found: list[str] = []
    for started_stored in (False, True):
        for crashes in itertools.product(CRASH_POINTS, CRASH_POINTS):
            for schedule in schedules():
                world = World(stored="seed" if started_stored else None)
                configure(world)
                run_schedule(world, crashes, schedule)
                count += 1
                found.extend(violations(world, started_stored)[:1])
    return count, found


def test_receipt_key_model() -> None:
    count, found = enumerate_all(lambda _world: None)
    assert count == 2 * len(CRASH_POINTS) ** 2 * len(schedules())
    assert found == []


def test_receipt_key_model_enumerates_a_crash_at_each_step() -> None:
    assert [point for point in CRASH_POINTS if point is not None] == [
        "before_lab_review",
        "after_sign_before_store",
        "after_store_before_post",
        "after_post",
    ]
    assert len(schedules()) == 3432


def test_receipt_key_model_catches_an_insert_that_overwrites() -> None:
    def overwrite(world: World) -> None:
        world.overwrite_on_insert = True

    _, found = enumerate_all(overwrite)
    assert found, "the check did not see two receipts written to one key"


def test_receipt_key_model_catches_a_post_of_the_own_candidate() -> None:
    def own(world: World) -> None:
        world.post_own_candidate = True

    _, found = enumerate_all(own)
    assert any("the key holds" in line for line in found)
