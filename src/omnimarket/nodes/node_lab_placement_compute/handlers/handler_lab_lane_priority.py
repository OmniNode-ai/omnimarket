# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""HandlerLabLanePriority -- the class a lane takes at placement (OMN-22124).

On 2026-11-02 a lane on omnibase_infra was refused PRIORITY-HOLDBACK with one pool slot free: the
remote-lane runner read only the lane's ticket for its class, so a repository the placement contract
lists under mvp-landing was charged to the lane as ``other``, and the slot held for mvp-landing was
held against an mvp-landing lane. A lane now takes, in order: the class its caller declared, the class
the contract lists its ticket (or its parent ticket) under, the class the contract lists its repository
under, else ``other``. The classes, repositories and tickets are the node contract's ``priority`` block.

OMN-22122: the classes are factory-unblock, factory-fix, feature and other, and a ticket carries its own
in a ``Class:`` line. A lane takes, in order: the class its caller declared, the ticket's ``Class:`` line, the
class the contract lists its ticket (or its parent ticket) under, the factory class when its ticket's parent is
a factory epic of the private overlay, the class its repository is listed under, else ``other``. A ticket read
without a ``Class:`` line falls back and the result reports the fallback.

The pool then admits or refuses the lane: no free slot refuses every class (the hosts refuse it); a
class leaves up to its contract ``holdback`` free slots to the classes above it that have a unit waiting.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Collection, Iterable, Mapping
from importlib import resources
from typing import Literal

import yaml
from pydantic import ValidationError

from omnimarket.handlers.node_overlay_reader import (
    NodeOverlayError,
    load_structured_overlay,
)
from omnimarket.models.model_priority_rules import (
    PRIORITY_OTHER,
    ModelPriorityRules,
    ModelRedCheckReading,
)
from omnimarket.nodes.node_lab_placement_compute.handlers.handler_lab_placement import (
    NODE_PACKAGE,
    HostTableError,
)
from omnimarket.nodes.node_lab_placement_compute.models.model_lab_lane_priority import (
    ModelLanePriorityOverlay,
    ModelLanePriorityRequest,
    ModelLanePriorityResult,
)


def load_priority_rules() -> ModelPriorityRules:
    """Read and check ``priority`` from this node's ``contract.yaml``."""
    text = resources.files(NODE_PACKAGE).joinpath("contract.yaml").read_text()
    block = (yaml.safe_load(text) or {}).get("priority")
    if not isinstance(block, dict):
        raise HostTableError("the lab placement contract declares no priority rules")
    try:
        return ModelPriorityRules.model_validate(block)
    except ValidationError as exc:
        raise HostTableError(
            f"the lab priority rules are invalid: {exc.errors()[0]['msg']}"
        ) from None


LANE_PRIORITY_OVERLAY_ID = "lane_priority"

#: A source's reader: the check ids it finds red; it raises when the source cannot be read.
RedCheckSource = Callable[[], Iterable[str]]


def read_red_checks(
    rules: ModelPriorityRules, readers: Mapping[str, RedCheckSource]
) -> ModelRedCheckReading:
    """The red declared checks, read from the contract's ``red_checks.sources`` in order (OMN-26624).

    The first source that reads decides. A source with no reader, or whose reader raises, is recorded and the next
    is tried; when none reads, the reading is ``unreadable`` with every reason and is never an empty red list."""
    declared = rules.red_checks
    if declared is None:
        return ModelRedCheckReading(read_from="none")
    why: list[str] = []
    for source in declared.sources:
        reader = readers.get(source)
        if reader is None:
            why.append(f"{source}: no reader")
            continue
        try:
            found = tuple(reader())
        except Exception as exc:
            why.append(f"{source}: {type(exc).__name__}: {str(exc)[:160]}")
            continue
        red = tuple(
            sorted({d for c in found if (d := declared.declared(str(c))) is not None})
        )
        return ModelRedCheckReading(red=red, read_from=source)
    return ModelRedCheckReading(unreadable="; ".join(why))


# A ticket's own line: ``Class: factory-fix``, as the ticket text quotes it (a ``> `` quote, a list marker or
# backticks around the name do not matter).
_CLASS_LINE = re.compile(
    r"^[ \t>*-]*Class:[ \t]*`?([A-Za-z][A-Za-z-]*)`?[ \t]*$", re.MULTILINE
)


def class_line(text: str | None) -> str | None:
    """The class the first ``Class:`` line of ``text`` names, lower-cased, else None."""
    found = _CLASS_LINE.search(text or "")
    return found.group(1).lower() if found else None


def load_factory_epics(environ: Mapping[str, str] | None = None) -> tuple[str, ...]:
    """The factory epics of the ``lane_priority`` overlay; none supplied means the overlay lists none."""
    try:
        overlay = load_structured_overlay(
            LANE_PRIORITY_OVERLAY_ID, ModelLanePriorityOverlay, environ
        )
    except NodeOverlayError as exc:
        if "is not configured" in str(exc):
            return ()
        raise
    return overlay.factory_epics


def priority_class_args(
    ticket_text: str | None,
    rules: ModelPriorityRules | None = None,
    *,
    brief_text: str | None = None,
    red_check_reading: ModelRedCheckReading | None = None,
) -> list[str]:
    """``["--priority-class", <class>]`` for the class a ticket's ``Class:`` line names, else ``[]``.

    A dispatcher appends this to the runner command: a ticket with no line, or a line naming a name the
    contract does not list, passes nothing, and the runner takes the repository rule and reports it.
    OMN-26624: a ticket text or brief naming a red declared check (``red_check_reading``) passes the contract's
    red-check class when that outranks the class line's."""
    rules = rules or load_priority_rules()
    named = class_line(ticket_text)
    cls = named if named in rules.classes else None
    declared = rules.red_checks
    request = ModelLanePriorityRequest(
        ticket_text=ticket_text,
        brief_text=brief_text,
        red_check_reading=red_check_reading,
    )
    if (
        declared is not None
        and HandlerLabLanePriority(rules).red_checks_of(request)
        and (
            cls is None
            or rules.classes.index(declared.class_) < rules.classes.index(cls)
        )
    ):
        cls = declared.class_
    return ["--priority-class", cls] if cls else []


class HandlerLabLanePriority:
    """COMPUTE handler: the class a lane takes and whether the pool's free slots admit it."""

    def __init__(self, rules: ModelPriorityRules | None = None) -> None:
        self._rules = rules or load_priority_rules()

    @property
    def handler_type(self) -> Literal["NODE_HANDLER"]:
        return "NODE_HANDLER"

    @property
    def handler_category(self) -> Literal["COMPUTE"]:
        return "COMPUTE"

    @property
    def rules(self) -> ModelPriorityRules:
        return self._rules

    def priority_class_of_repo(self, repo: str | None) -> str:
        """The class the contract lists ``repo`` under, else ``other``."""
        return self._rules.class_of_repo(repo)

    def red_checks_named(self, request: ModelLanePriorityRequest) -> tuple[str, ...]:
        """The declared delegation checks the lane's ticket text or brief names, red or not."""
        declared = self._rules.red_checks
        if declared is None:
            return ()
        return declared.named_in(request.ticket_text, request.brief_text)

    def red_checks_of(self, request: ModelLanePriorityRequest) -> tuple[str, ...]:
        """The checks the lane names that the reading shows red; an unread or unreadable result shows none."""
        reading = request.red_check_reading
        if reading is None or reading.unreadable is not None:
            return ()
        red = {c.upper() for c in reading.red}
        return tuple(c for c in self.red_checks_named(request) if c.upper() in red)

    def red_check_report(self, request: ModelLanePriorityRequest) -> str | None:
        """What to report when the lane names a declared check and its result was not read, else None."""
        named = self.red_checks_named(request)
        reading = request.red_check_reading
        if not named:
            return None
        if reading is None:
            return (
                f"RED-CHECK-UNREAD: the lane names {', '.join(named)} and the caller read no check result; "
                "the check is not read as green"
            )
        if reading.unreadable is not None:
            return (
                f"RED-CHECK-UNREADABLE: the lane names {', '.join(named)} and no check result could be read "
                f"({reading.unreadable}); the check is not read as green"
            )
        return None

    def classify(self, request: ModelLanePriorityRequest) -> tuple[str, str]:
        """The lane's class and its source: red-check, declared, class-line, ticket, factory-epic, repository or default.

        A red check named by the lane takes the contract's class only when that outranks the class the lane would take."""
        cls, source = self._classify_without_checks(request)
        declared = self._rules.red_checks
        if declared is not None and self.red_checks_of(request):
            classes = self._rules.classes
            if classes.index(declared.class_) < classes.index(cls):
                return declared.class_, "red-check"
        return cls, source

    def _classify_without_checks(
        self, request: ModelLanePriorityRequest
    ) -> tuple[str, str]:
        rules = (
            self._rules.with_repos(request.private_repos)
            if request.private_repos
            else self._rules
        )
        if request.declared in rules.classes:
            return str(request.declared), "declared"
        named = class_line(request.ticket_text)
        if named in rules.classes:
            return str(named), "class-line"
        by_ticket = rules.class_of_ticket(request.ticket, request.parent)
        if by_ticket != PRIORITY_OTHER:
            return by_ticket, "ticket"
        epics = {e.strip().upper() for e in request.factory_epics}
        if str(request.parent or "").strip().upper() in epics - {""}:
            return rules.factory_class, "factory-epic"
        by_repo = rules.class_of_repo(request.repo)
        if by_repo != PRIORITY_OTHER:
            return by_repo, "repository"
        return PRIORITY_OTHER, "default"

    def fallback_report(
        self, request: ModelLanePriorityRequest, cls: str, source: str
    ) -> str | None:
        """What to report when the ticket's text was read and gave no class, else None."""
        if request.ticket_text is None or source in (
            "declared",
            "class-line",
            "red-check",
        ):
            return None
        named = class_line(request.ticket_text)
        if named is None:
            why = "CLASS-LINE-MISSING: the ticket text has no Class line"
        else:
            why = f"CLASS-LINE-UNKNOWN: the ticket's Class line names {named!r}, not one of {list(self._rules.classes)}"
        return f"{why}; the lane takes {cls} from {source}"

    def holdback_refusal(
        self, priority: str, free_slots: int, waiting: Collection[str]
    ) -> str | None:
        """Why a unit of class ``priority`` takes none of the pool's ``free_slots``, else None.

        A class leaves one free slot to each class above it that has a unit waiting, up to its contract
        ``holdback``. With no free slot the hosts refuse every class, so the holdback refuses only while
        0 < free_slots <= its share. An unknown class is judged as ``other``."""
        classes = self._rules.classes
        cls = priority if priority in classes else PRIORITY_OTHER
        above = [c for c in classes[: classes.index(cls)] if c in waiting]
        if 0 < free_slots <= min(len(above), self._rules.holdback[cls]):
            return (
                f"PRIORITY-HOLDBACK: the pool has {free_slots} free slot(s), held for "
                f"{', '.join(above)}; a {cls} unit waits for the next placement"
            )
        return None

    def handle(self, request: ModelLanePriorityRequest) -> ModelLanePriorityResult:
        cls, source = self.classify(request)
        refusal: str | None = None
        if request.free_slots == 0:
            refusal = "NO-FREE-SLOT: the pool has no free slot, whatever the class"
        elif request.free_slots is not None:
            refusal = self.holdback_refusal(cls, request.free_slots, request.waiting)
        return ModelLanePriorityResult(
            priority_class=cls,
            source=source,
            refusal=refusal,
            fallback=self.fallback_report(request, cls, source),
            red_checks=self.red_checks_of(request),
            red_check_report=self.red_check_report(request),
        )


__all__ = [
    "HandlerLabLanePriority",
    "class_line",
    "load_factory_epics",
    "load_priority_rules",
    "priority_class_args",
    "read_red_checks",
]
