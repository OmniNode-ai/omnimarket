# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

# Copyright (c) 2026 OmniNode Team
"""Proxy contains/getitem consistency + durable-row-keyed dedup (OMN-14721).

Root cause (docs/tracking/2026-07-17-delegation-routing-publish-drop-rootcause.md):
``DelegationWorkflowStateProxy`` had an inconsistency between ``__contains__`` and
``__getitem__`` under a bound state_io dispatch — ``__contains__`` returned ``True``
whenever ``cid`` was in ``_cache`` (e.g. a workflow THIS dispatch just created via
``__setitem__`` under a ``None`` source), while ``__getitem__`` raised ``KeyError``
for that same ``cid`` because the bound row's payload was still ``None``. The FSM
dedup guard in ``handle_delegation_request`` is exactly ``if cid in self._workflows:
workflow = self._workflows[cid]`` — so a second in-process leg for the same
correlation could pass the ``in`` check and then blow up on the read, and the
committing leg ended up severed from its ``ModelRoutingIntent`` emission (the
row stalled at ``RECEIVED`` with an empty outbox).

The fix keys both operations on the SAME durable-row-plus-matching-cache predicate
(``_lookup_bound``): ``cid in proxy`` is true iff ``proxy[cid]`` succeeds. These
tests drive the REAL ``HandlerDelegationWorkflow`` + real proxy + real
``CONTEXTVAR_STATE_IO_ROWS`` binding — the actual cross-repo seam.

Verified RED against pre-fix ``state_codec.py`` (with the OMN-14721 change removed
both tests FAIL: ``test_created_workflow_is_consistently_readable_within_bound_dispatch``
sees ``__getitem__`` raise ``KeyError`` while ``__contains__`` is ``True``, and the
second-leg test raises ``KeyError`` instead of re-emitting — see the PR body for the
captured pre-fix output).
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from omnimarket.nodes.node_delegation_orchestrator.enums import EnumDelegationState
from omnimarket.nodes.node_delegation_orchestrator.handlers.handler_delegation_workflow import (
    HandlerDelegationWorkflow,
)
from omnimarket.nodes.node_delegation_orchestrator.models.model_routing_intent import (
    ModelRoutingIntent,
)
from omnimarket.nodes.node_delegation_orchestrator.state_codec import get_default_proxy

# Reuse the established bound-ContextVar + request helpers from the OMN-14208 suite.
from tests.unit.delegation.test_delegation_state_proxy_omn14208 import (
    _bind_infra_state_io_rows,
    _make_request,
)


@pytest.fixture(autouse=True)
def _isolate_default_proxy_cache() -> object:
    """The default proxy is a process-wide singleton — clear its cache around
    each test so a created workflow never leaks into another test."""
    get_default_proxy()._cache.clear()
    yield
    get_default_proxy()._cache.clear()


@pytest.mark.unit
class TestProxyContainsGetitemConsistency:
    def test_created_workflow_is_consistently_readable_within_bound_dispatch(
        self,
    ) -> None:
        """A workflow created THIS dispatch must satisfy both ``in`` and ``[]``.

        Pre-fix ``__contains__`` returned ``True`` (cache hit) while ``__getitem__``
        raised ``KeyError`` (bound payload still ``None``) — the inconsistency that
        crashed the FSM dedup guard's ``workflow = self._workflows[cid]`` read.
        """
        cid = uuid4()
        handler = HandlerDelegationWorkflow()

        with _bind_infra_state_io_rows({str(cid): (None, 0)}):
            # A fresh request creates the workflow via __setitem__ (None source).
            intents = handler.handle_delegation_request(_make_request(cid))
            assert len(intents) == 1

            # contains and getitem must AGREE — both succeed.
            assert cid in handler.workflows
            workflow = handler.workflows[cid]  # pre-fix: raised KeyError
            assert workflow.state == EnumDelegationState.RECEIVED
            assert workflow.routing_intent_replayed is False

    def test_second_in_process_leg_rereads_row_and_re_emits_routing_intent(
        self,
    ) -> None:
        """A second same-correlation leg in one binding re-emits, never drops.

        This is the divergence the live incident hit: the committing leg read back
        the just-created workflow and (pre-fix) crashed on ``KeyError``, so its
        result carried an EMPTY batch and the row stalled at RECEIVED. Post-fix the
        second leg consistently reads the RECEIVED workflow and takes the replay
        branch, so it carries a ``ModelRoutingIntent`` — the committing leg is never
        severed from its emission.
        """
        cid = uuid4()
        handler = HandlerDelegationWorkflow()

        with _bind_infra_state_io_rows({str(cid): (None, 0)}):
            first = handler.handle_delegation_request(_make_request(cid))
            assert len(first) == 1
            assert isinstance(first[0], ModelRoutingIntent)

            # Second in-process execution for the SAME correlation, SAME binding.
            second = handler.handle_delegation_request(_make_request(cid))
            # Pre-fix: this raised KeyError (contains True, getitem KeyError).
            # Post-fix: the replay branch fires and re-emits the routing intent.
            assert len(second) == 1, (
                "the second leg must re-emit the routing intent, not silently "
                f"drop it (got {second!r})"
            )
            assert isinstance(second[0], ModelRoutingIntent)

            # The replay flag was set on the durably-cached workflow (not a
            # divergent object) — i.e. the two legs share one row identity.
            assert handler.workflows[cid].routing_intent_replayed is True
