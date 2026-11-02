# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""HandlerDevTagPinSet - choose the dev-tag set of a consumer's siblings that co-resolves (OMN-21895).

The nightly train (OMN-19998) used to move each sibling to its own newest green
dev tag. uv honours a git dependency's sources, so when omnimarket pins
omnibase_infra by a dev tag whose pyproject.toml pins omnibase-core by another
dev tag, moving omnibase-core alone fails with "Requirements contain
conflicting URLs for package omnibase-core" (omnimarket#4588 to #4624).

This is the pure half of the fix: given every sibling the consumer pins by dev
tag, its candidate tags newest first with their CI verdict, and the sibling
tags each candidate itself pins, it returns the set of moves in which every
pinned sibling tag is the tag the others name. The preference is the newest
tag for each sibling, most moves first, so a dependent (omnibase_infra) is
moved to its newest green tag together with the dependency tag (omnibase-core)
that its commit names. A sibling never moves backwards, only green tags are
taken, and a move that no tag set can carry is held back with the conflicting
URLs rather than opened as a PR.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

from omnimarket.nodes.node_dev_tag_pin_mover_compute.models.model_dev_tag_pin_set import (
    EnumPinSetOutcome,
    ModelPinSetConflict,
    ModelPinSetHeldBack,
    ModelPinSetMove,
    ModelPinSetRequest,
    ModelPinSetResult,
    ModelPinSetSibling,
)

_DEV_TAG = re.compile(r"^dev-(?P<stamp>\d{14})-(?P<sha12>[0-9a-f]{12})$")
_REPO = re.compile(r"/(?P<repo>[^/]+?)(?:\.git)?$")


@dataclass(frozen=True)
class _Option:
    tag: str
    requires: tuple[tuple[str, str], ...]
    is_current: bool


def _stamp(tag: str) -> str:
    match = _DEV_TAG.match(tag)
    return match.group("stamp") if match else ""


def _repo(url: str) -> str:
    match = _REPO.search(url)
    return match.group("repo") if match else url


def _url(sibling: ModelPinSetSibling, tag: str) -> str:
    """The URL uv names for a tag pin of ``sibling``."""
    return f"{sibling.repository_url}?tag={tag}"


def _options(sibling: ModelPinSetSibling) -> list[_Option]:
    """Green tags newer than the pin, newest first, then the pin itself."""
    current = _stamp(sibling.current_tag)
    newer = sorted(
        (c for c in sibling.candidates if c.green is True and _stamp(c.tag) > current),
        key=lambda c: _stamp(c.tag),
        reverse=True,
    )
    options = [_Option(c.tag, tuple(sorted(c.requires.items())), False) for c in newer]
    options.append(
        _Option(
            sibling.current_tag, tuple(sorted(sibling.current_requires.items())), True
        )
    )
    return options


def _conflicts(
    consumer: str,
    siblings: dict[str, ModelPinSetSibling],
    chosen: dict[str, _Option],
) -> list[ModelPinSetConflict]:
    """Every pair of pins in ``chosen`` that name one package at two tags."""
    found: list[ModelPinSetConflict] = []
    for name in sorted(chosen):
        option = chosen[name]
        for dep, wanted in option.requires:
            dep_option = chosen.get(dep)
            if dep_option is None or dep_option.tag == wanted:
                continue
            dep_sibling = siblings[dep]
            sha12 = _DEV_TAG.match(option.tag) or _DEV_TAG.match(wanted)
            holder = _repo(siblings[name].repository_url)
            found.append(
                ModelPinSetConflict(
                    package=dep,
                    urls=(
                        _url(dep_sibling, dep_option.tag),
                        _url(dep_sibling, wanted),
                    ),
                    detail=(
                        f"{holder} {sha12.group('sha12') if sha12 else option.tag} "
                        f"pins {dep} {wanted}, {consumer} pins {dep_option.tag}; "
                        "move both pins in one change"
                    ),
                )
            )
    return found


def _consistent(chosen: dict[str, _Option], name: str, option: _Option) -> bool:
    """``option`` for ``name`` agrees with every sibling already chosen."""
    for dep, wanted in option.requires:
        other = chosen.get(dep)
        if other is not None and other.tag != wanted:
            return False
    for other in chosen.values():
        for dep, wanted in other.requires:
            if dep == name and option.tag != wanted:
                return False
    return True


class HandlerDevTagPinSet:
    """COMPUTE handler: the co-resolving tag set of one consumer's siblings."""

    @property
    def handler_type(self) -> Literal["NODE_HANDLER"]:
        return "NODE_HANDLER"

    @property
    def handler_category(self) -> Literal["COMPUTE"]:
        return "COMPUTE"

    def handle(self, request: ModelPinSetRequest) -> ModelPinSetResult:
        siblings = {s.distribution: s for s in request.siblings}
        names = sorted(siblings)
        options = {name: _options(siblings[name]) for name in names}

        best: dict[str, _Option] | None = None
        best_score: tuple[int, tuple[str, ...]] = (-1, ())

        def walk(index: int, chosen: dict[str, _Option]) -> None:
            nonlocal best, best_score
            if index == len(names):
                moved = sum(1 for o in chosen.values() if not o.is_current)
                score = (moved, tuple(_stamp(chosen[n].tag) for n in names))
                if score > best_score:
                    best, best_score = dict(chosen), score
                return
            name = names[index]
            for option in options[name]:
                if _consistent(chosen, name, option):
                    chosen[name] = option
                    walk(index + 1, chosen)
                    del chosen[name]

        walk(0, {})

        # The train's old behaviour: every sibling at its own newest green tag.
        naive = {name: options[name][0] for name in names}
        naive_conflicts = _conflicts(request.consumer, siblings, naive)

        if best is None:
            return ModelPinSetResult(
                outcome=EnumPinSetOutcome.NO_CO_RESOLVING_SET,
                unchanged=tuple(names),
                held_back=tuple(
                    ModelPinSetHeldBack(
                        distribution=c.package, tag=naive[c.package].tag, conflict=c
                    )
                    for c in naive_conflicts
                ),
                detail="no dev-tag set of the siblings pins each other consistently",
            )

        moves: list[ModelPinSetMove] = []
        unchanged: list[str] = []
        for name in names:
            option = best[name]
            sibling = siblings[name]
            if option.is_current:
                unchanged.append(name)
                continue
            old, new = _stamp(sibling.current_tag), _stamp(option.tag)
            rolled = tuple(
                c.tag
                for c in sorted(
                    sibling.candidates, key=lambda c: _stamp(c.tag), reverse=True
                )
                if old < _stamp(c.tag) <= new
            )
            moves.append(
                ModelPinSetMove(
                    distribution=name,
                    repository_url=sibling.repository_url,
                    old_tag=sibling.current_tag,
                    new_tag=option.tag,
                    rolled=rolled,
                    requires={
                        dep: tag for dep, tag in option.requires if dep in siblings
                    },
                )
            )

        held: list[ModelPinSetHeldBack] = []
        for conflict in naive_conflicts:
            package = conflict.package
            if naive[package].tag != best[package].tag:
                held.append(
                    ModelPinSetHeldBack(
                        distribution=package, tag=naive[package].tag, conflict=conflict
                    )
                )
        if moves:
            outcome = EnumPinSetOutcome.MOVED
        elif held:
            outcome = EnumPinSetOutcome.NO_CO_RESOLVING_SET
        else:
            outcome = EnumPinSetOutcome.UNCHANGED
        return ModelPinSetResult(
            outcome=outcome,
            moves=tuple(moves),
            unchanged=tuple(unchanged),
            held_back=tuple(held),
            detail=(
                "no newer green dev tag set co-resolves; nothing moves"
                if outcome is EnumPinSetOutcome.NO_CO_RESOLVING_SET
                else ""
            ),
        )


def render_pin_set_body(result: ModelPinSetResult) -> str:
    """The one PR body a night's bump opens with: every moved pin, together."""
    lines = ["## Dev-tag pin train", ""]
    if not result.moves:
        lines.append(
            "The train moved no sibling: no newer green dev tag set co-resolves."
        )
    for move in result.moves:
        lines.append(
            f"- `{move.distribution}` ({_repo(move.repository_url)}): "
            f"`{move.old_tag}` -> `{move.new_tag}`, rolling {len(move.rolled)} "
            "dev tag(s): " + ", ".join(f"`{tag}`" for tag in move.rolled)
        )
    paired = [m for m in result.moves if m.requires]
    if paired:
        lines += ["", "### Moved together", ""]
        for move in paired:
            for dep, tag in sorted(move.requires.items()):
                lines.append(
                    f"- `{move.distribution}` `{move.new_tag}` pins `{dep}` "
                    f"`{tag}`; both pins move in this change"
                )
    if result.held_back:
        lines += [
            "",
            "### Held back (the siblings cannot co-resolve at these tags)",
            "",
        ]
        for item in result.held_back:
            lines += ["```", item.conflict.render(), "```"]
    if result.unchanged:
        lines += ["", "### Left where they were", ""]
        lines += [f"- `{name}`" for name in result.unchanged]
    return "\n".join(lines) + "\n"


__all__ = ["HandlerDevTagPinSet", "render_pin_set_body"]
