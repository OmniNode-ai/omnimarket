# SPDX-License-Identifier: MIT
"""The ``onex scm red-facts`` read: the filler's red-facts records of a PR head.

Kept beside, not inside, ``handler_scm_local_state`` and ``handler_scm_dispatch``:
those are baselined files that only shrink. This handler answers the one read
and hands every other read to the local-state handler.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Final

from pydantic import JsonValue

from omnimarket.handlers.node_overlay_reader import NodeOverlayError
from omnimarket.handlers.scm.handler_scm_local_state import (
    HandlerScmLocalState,
    _parse_stamp,
)
from omnimarket.models.scm import (
    EnumScmLocalRead,
    ModelScmLocalReadRequest,
    ModelScmLocalReadResult,
    ModelScmLocalVerb,
)
from omnimarket.nodes.node_job_log_filler_effect.handlers.adapter_red_facts import (
    RedFactsStore,
    answer_red_facts,
    load_red_facts_overlay,
)

RED_FACTS_VERB: Final[ModelScmLocalVerb] = ModelScmLocalVerb(
    verb="red-facts",
    read=EnumScmLocalRead.RED_FACTS,
    replaces="onex scm job-log / job-log-cached raw log reads for the failing line",
)


class HandlerScmRedFacts(HandlerScmLocalState):
    """Local-state handler that also answers the red-facts read."""

    def __init__(
        self,
        now: datetime | None = None,
        red_facts_store: Callable[[], RedFactsStore] | None = None,
    ) -> None:
        super().__init__(now)
        self._red_facts_store = red_facts_store

    def handle(self, request: ModelScmLocalReadRequest) -> ModelScmLocalReadResult:
        if request.read is not EnumScmLocalRead.RED_FACTS:
            return super().handle(request)
        state = self._load(request.watcher_state_path)
        if isinstance(state, str):
            return self._unanswered(request, state)
        tick = state.get("last_tick")
        if not self._fresh(tick):
            return self._unanswered(
                request, f"the watcher state is stale (last_tick {tick})"
            )
        key = f"{request.repo.split('/', 1)[1]}#{request.pr_number}"
        record = self._dict(self._dict(state.get("prs")).get(key))
        facts = self._dict(record.get("facts"))
        if not facts:
            return self._unanswered(
                request, f"the watcher state holds no record of {key}"
            )
        head = self._str(facts.get("head_sha"))
        if head is None or request.pr_number is None:
            return self._unanswered(request, "the watcher record has no head commit")
        read_at = _parse_stamp(self._dict(record.get("ci")).get("read_at"))
        age = (
            max(0, int(((self._now or datetime.now(UTC)) - read_at).total_seconds()))
            if read_at is not None
            else None
        )
        try:
            store = (
                self._red_facts_store()
                if self._red_facts_store
                else RedFactsStore.from_overlay(load_red_facts_overlay())
            )
            answer = answer_red_facts(
                store=store,
                repo=request.repo,
                pr_number=request.pr_number,
                head_sha=head,
                head_age_seconds=age,
            )
        except (NodeOverlayError, ValueError, OSError) as exc:
            return self._unanswered(
                request, f"the red-facts store is unreadable: {exc}"
            )
        result: dict[str, JsonValue] = answer.model_dump(mode="json")
        return ModelScmLocalReadResult(
            read=request.read,
            repo=request.repo,
            pr_number=request.pr_number,
            answered=True,
            source="watcher",
            watcher_tick=tick if isinstance(tick, str) else None,
            result=result,
        )


__all__: list[str] = ["RED_FACTS_VERB", "HandlerScmRedFacts"]
