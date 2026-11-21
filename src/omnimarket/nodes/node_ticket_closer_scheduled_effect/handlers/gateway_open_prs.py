# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The GitHub gateway bound as the ticket closer's open-pull-request source (OMN-25459).

``GatewayOpenPullRequests`` answers ``open_for`` from one ``open_pr_facts`` read of
``node_github_repo_gateway_effect`` per scope repository, so the closer needs no copy of the
PR watcher's state file on the host it runs on. Each answer is folded into the watcher-state
record shape and matched by ``closer_pr_rules.open_prs``, the same rule the closeout sweep
applies to the watcher state: a ticket id in a pull request's title or head branch, the
evidence repository and ``evidence(...)`` titles left out.

A read that cannot be taken as complete is ``None``, never an empty set: a gateway that does
not answer, an answer that is not the ``open_pr_facts`` result and an answer cut at the
request's limit each leave the open pull requests unread, and the candidate source reads its
candidates as unread instead of deciding every ticket's pull requests are merged.
"""

from __future__ import annotations

import logging
from typing import Any

from omnimarket.handlers.scm.handler_gateway_read_client import GatewayReadError
from omnimarket.models.closeout_scope_overlay import ModelCloseoutScopeOverlay
from omnimarket.models.model_github_gateway_io import (
    MAX_LIST_LIMIT,
    EnumGithubGatewayOperation,
    ModelGithubGatewayRequest,
    ModelOpenPrFactsResult,
)
from omnimarket.nodes.node_dod_closeout_sweep_compute.handlers.closer_pr_rules import (
    open_prs,
)
from omnimarket.nodes.node_ticket_closer_scheduled_effect.handlers.closer_protocols import (
    ProtocolGatewayRead,
)

_LOG = logging.getLogger(__name__)


class GatewayOpenPullRequests:
    """The real backend open-pull-request source: the GitHub gateway, read over the bus."""

    def __init__(
        self, gateway: ProtocolGatewayRead, scope: ModelCloseoutScopeOverlay
    ) -> None:
        self._gateway = gateway
        self._scope = scope

    def open_for(self, ticket_ids: frozenset[str]) -> frozenset[str] | None:
        records: dict[str, Any] = {}
        for repository in self._scope.pr_repositories:
            slug = f"{self._scope.github_owner}/{repository.name}"
            result = self._read(slug)
            if result is None:
                return None
            for fact in result.prs:
                records[f"{repository.name}#{fact.number}"] = {
                    "facts": {
                        "state": "OPEN",
                        "repo": repository.name,
                        "number": fact.number,
                        "title": fact.title,
                        "head_ref": fact.head_ref,
                    }
                }
        opened = open_prs({"prs": records}, self._scope)
        return frozenset(ticket_id for ticket_id in ticket_ids if ticket_id in opened)

    def _read(self, slug: str) -> ModelOpenPrFactsResult | None:
        request = ModelGithubGatewayRequest(
            operation=EnumGithubGatewayOperation.OPEN_PR_FACTS,
            repo=slug,
            limit=MAX_LIST_LIMIT,
        )
        try:
            result = self._gateway.read(request)
        except GatewayReadError as exc:
            _LOG.warning("open pull requests of %s unread: %s", slug, exc)
            return None
        if not isinstance(result, ModelOpenPrFactsResult):
            _LOG.warning(
                "open pull requests of %s unread: the gateway answered %s, not open_pr_facts",
                slug,
                result.operation,
            )
            return None
        if result.truncated:
            _LOG.warning(
                "open pull requests of %s unread: the answer was cut at %d of %s",
                slug,
                len(result.prs),
                result.total_count,
            )
            return None
        return result
