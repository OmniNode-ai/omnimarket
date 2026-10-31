# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Handler of node_dod_criteria_check_effect (OMN-21676).

The hosted ``repo-evidence / dod-verify`` job runs on a runner that holds no Linear credential
and, by design, no repository secret. It therefore cannot compare the criteria a ticket
contract was accepted against with the criteria the live ticket carries, and records that
comparison as delegated (``criteria_delegated`` on its verdict). This node is where the
comparison is made: the landing controller runs it on a lab host before it merges or enqueues
a pull request, at the head it is about to merge.

One request names a pull request and the head sha the caller will merge. The handler

1. reads the pull request and refuses (``UNREADABLE``) a head that moved past the request;
2. takes the tickets from the title, as the hosted gate does, and reads each
   ``contracts/<ticket>.yaml`` at that head;
3. for a contract that records criteria (``ac_bindings`` or a ``requirements`` acceptance id,
   the verifier's own ``extract_criteria_pins``), reads the live ticket through the project
   tracker the ticketing nodes use, twice, and compares with the verifier's own
   ``evaluate_criteria``: the rule is not restated here.

A contract that records no criteria, or a ticket with no contract at the head, is
``NOT_ENGAGED``: the hosted gate already refuses the missing contract, and there is nothing to
drift from. Drift blocks the merge. So does anything unreadable, because an unreadable ticket is
never an unchanged one. The Linear credential is the contract-declared ``LINEAR_API_KEY``,
resolved from the lab key store at the effect boundary, and the GitHub credential is the
declared ``GITHUB_TOKEN``; neither is returned, logged or placed in a reason.
"""

from __future__ import annotations

import base64
import binascii
import contextlib
import logging
import re
from collections.abc import Awaitable, Callable
from pathlib import Path
from urllib.parse import quote

import yaml

from omnimarket.github_landing.github_landing_requests import (
    pull_request_request,
    split_repository,
)
from omnimarket.github_landing.github_landing_transport import (
    GithubLandingTransportError,
    UrllibGithubLandingTransport,
)
from omnimarket.github_landing.model_github_http_exchange import (
    ModelGithubHttpRequest,
    ModelGithubHttpResponse,
)
from omnimarket.inference.secret_store_resolver import resolve_api_key_async
from omnimarket.models.model_criteria_pins import (
    ModelCriteriaPins,
)
from omnimarket.nodes.contract_topics import (
    contract_publish_topics,
    contract_secret_ref,
)
from omnimarket.nodes.node_dod_criteria_check_effect.models import (
    EnumDodCriteriaVerdict,
    ModelDodCriteriaCheckRequest,
    ModelDodCriteriaCheckResult,
    ModelDodCriteriaTicketVerdict,
)
from omnimarket.nodes.node_dod_criteria_check_effect.protocols import (
    ProtocolCriteriaEventPublisher,
    ProtocolCriteriaGithubTransport,
)
from omnimarket.nodes.node_dod_verify.services.criteria_drift import (
    evaluate_criteria,
    extract_criteria_pins,
)
from omnimarket.nodes.node_dod_verify.services.criteria_ticket_read import (
    ProtocolCriteriaTracker,
    read_ticket_through_tracker,
)
from omnimarket.nodes.node_dod_verify.services.linear_criteria_tracker import (
    linear_project_tracker,
)

_log = logging.getLogger(__name__)
_CONTRACT_PATH = Path(__file__).resolve().parents[1] / "contract.yaml"
_GITHUB_SECRET = "GITHUB_TOKEN"
# The hosted gate takes its tickets from the title with the same pattern.
_TICKET = re.compile(r"OMN-[0-9]+")
_NOT_FOUND = 404
_OK = 200

TrackerFactory = Callable[[], Awaitable[ProtocolCriteriaTracker]]


class _UnreadableError(Exception):
    """Internal: a read the check cannot continue without failed; the message is the reason."""


class HandlerDodCriteriaCheck:
    """EFFECT: compare the live criteria of a pull request's tickets with their contracts."""

    def __init__(
        self,
        transport: ProtocolCriteriaGithubTransport | None = None,
        tracker_factory: TrackerFactory | None = None,
        event_bus: ProtocolCriteriaEventPublisher | None = None,
    ) -> None:
        self._transport = transport
        self._tracker_factory = tracker_factory or _linear_tracker
        self._event_bus = event_bus
        self._completed_topic = contract_publish_topics(_CONTRACT_PATH)[0]

    async def handle(
        self, request: ModelDodCriteriaCheckRequest
    ) -> ModelDodCriteriaCheckResult:
        """Check one pull request head; a failure of any read blocks, never passes."""
        try:
            tickets = await self._check(request)
            result = self._result(request, tickets, "")
        except _UnreadableError as error:
            result = self._result(request, (), str(error))
        except Exception as error:
            _log.error("criteria check %s#%d failed", request.repo, request.pr_number)
            result = self._result(
                request, (), f"the check could not run ({type(error).__name__})"
            )
        await self._publish(result)
        return result

    async def _publish(self, result: ModelDodCriteriaCheckResult) -> None:
        """The completed event; a publish failure is logged and never changes the verdict."""
        if self._event_bus is None:
            return
        try:
            await self._event_bus.publish(
                self._completed_topic,
                key=f"{result.repo}#{result.pr_number}".encode(),
                value=result.model_dump_json().encode(),
            )
        except Exception:
            _log.exception(
                "criteria check: failed to publish on %s", self._completed_topic
            )

    async def _check(
        self, request: ModelDodCriteriaCheckRequest
    ) -> tuple[ModelDodCriteriaTicketVerdict, ...]:
        transport = self._transport or await _live_transport()
        title = await self._read_title(transport, request)
        ticket_ids = sorted(set(_TICKET.findall(title)))
        if not ticket_ids:
            return ()
        verdicts: list[ModelDodCriteriaTicketVerdict] = []
        tracker: ProtocolCriteriaTracker | None = None
        try:
            for ticket_id in ticket_ids:
                path = f"contracts/{ticket_id}.yaml"
                text = await self._read_contract(transport, request, path)
                if text is None:
                    verdicts.append(
                        _verdict(
                            ticket_id,
                            path,
                            EnumDodCriteriaVerdict.NOT_ENGAGED,
                            "no contract at this head; the hosted gate refuses that",
                        )
                    )
                    continue
                pins = _pins(text)
                if isinstance(pins, str):
                    verdicts.append(
                        _verdict(
                            ticket_id, path, EnumDodCriteriaVerdict.UNREADABLE, pins
                        )
                    )
                    continue
                if pins is None:
                    verdicts.append(
                        _verdict(
                            ticket_id,
                            path,
                            EnumDodCriteriaVerdict.NOT_ENGAGED,
                            "the contract records no criteria to compare",
                        )
                    )
                    continue
                if tracker is None:
                    tracker = await self._open_tracker()
                before = await read_ticket_through_tracker(tracker, ticket_id)
                after = await read_ticket_through_tracker(tracker, ticket_id)
                check = evaluate_criteria(pins, ticket_id, before, after)
                if check.unavailable_reason:
                    verdicts.append(
                        _verdict(
                            ticket_id,
                            path,
                            EnumDodCriteriaVerdict.UNREADABLE,
                            f"the live ticket could not be read ({check.unavailable_reason})",
                        )
                    )
                elif check.refused:
                    verdicts.append(
                        ModelDodCriteriaTicketVerdict(
                            ticket_id=ticket_id,
                            contract_path=path,
                            verdict=EnumDodCriteriaVerdict.DRIFT,
                            detail=check.message(ticket_id),
                            criteria_revision=check.criteria_revision,
                            drift=check.drift,
                            amendment=check.amendment,
                        )
                    )
                else:
                    verdicts.append(
                        ModelDodCriteriaTicketVerdict(
                            ticket_id=ticket_id,
                            contract_path=path,
                            verdict=EnumDodCriteriaVerdict.PASS,
                            detail="the live criteria are the ones the contract was accepted against",
                            criteria_revision=check.criteria_revision,
                        )
                    )
        finally:
            if tracker is not None:
                with contextlib.suppress(
                    Exception
                ):  # a failed close changes no verdict
                    await tracker.close()
        return tuple(verdicts)

    async def _open_tracker(self) -> ProtocolCriteriaTracker:
        try:
            return await self._tracker_factory()
        except Exception as error:
            raise _UnreadableError(
                f"no ticket tracker could be built ({type(error).__name__}); "
                "is LINEAR_API_KEY in the lab key store?"
            ) from error

    async def _read_title(
        self,
        transport: ProtocolCriteriaGithubTransport,
        request: ModelDodCriteriaCheckRequest,
    ) -> str:
        response = await _send(
            transport, pull_request_request(request.repo, request.pr_number)
        )
        body = response.body
        if response.status != _OK or body is None:
            raise _UnreadableError(
                f"the pull request could not be read (HTTP {response.status})"
            )
        head = body.get("head")
        head_sha = head.get("sha") if isinstance(head, dict) else None
        if head_sha != request.head_sha:
            raise _UnreadableError(
                "the pull request head is not the one the caller will merge "
                f"({str(head_sha)[:12]} against {request.head_sha[:12]})"
            )
        if body.get("state") != "open" or body.get("merged") is True:
            raise _UnreadableError("the pull request is not open")
        title = body.get("title")
        if not isinstance(title, str):
            raise _UnreadableError("the pull request carries no title")
        return title

    async def _read_contract(
        self,
        transport: ProtocolCriteriaGithubTransport,
        request: ModelDodCriteriaCheckRequest,
        path: str,
    ) -> str | None:
        owner, name = split_repository(request.repo)
        response = await _send(
            transport,
            ModelGithubHttpRequest(
                method="GET",
                path=f"/repos/{owner}/{name}/contents/{quote(path)}?ref={request.head_sha}",
            ),
        )
        if response.status == _NOT_FOUND:
            return None
        body = response.body
        if response.status != _OK or body is None:
            raise _UnreadableError(f"{path} could not be read (HTTP {response.status})")
        content = body.get("content")
        if body.get("encoding") != "base64" or not isinstance(content, str):
            raise _UnreadableError(f"{path} came back in a form that cannot be read")
        try:
            return base64.b64decode(content).decode("utf-8")
        except (binascii.Error, UnicodeDecodeError) as error:
            raise _UnreadableError(f"{path} is not readable text") from error

    @staticmethod
    def _result(
        request: ModelDodCriteriaCheckRequest,
        tickets: tuple[ModelDodCriteriaTicketVerdict, ...],
        failure: str,
    ) -> ModelDodCriteriaCheckResult:
        if failure:
            overall = EnumDodCriteriaVerdict.UNREADABLE
            reason = f"CRITERIA_UNREADABLE: {failure}"
        else:
            overall = max(
                (t.verdict for t in tickets),
                key=lambda v: v.severity,
                default=EnumDodCriteriaVerdict.NOT_ENGAGED,
            )
            blocking = [
                t for t in tickets if t.verdict is overall and overall.blocks_merge
            ]
            reason = (
                "; ".join(f"{t.ticket_id}: {t.detail}" for t in blocking)
                if blocking
                else ""
            )
            if overall is EnumDodCriteriaVerdict.UNREADABLE and reason:
                reason = f"CRITERIA_UNREADABLE: {reason}"
        return ModelDodCriteriaCheckResult(
            correlation_id=request.correlation_id,
            repo=request.repo,
            pr_number=request.pr_number,
            head_sha=request.head_sha,
            verdict=overall,
            merge_blocked=overall.blocks_merge,
            reason=reason,
            tickets=tickets,
        )


def _verdict(
    ticket_id: str, path: str, verdict: EnumDodCriteriaVerdict, detail: str
) -> ModelDodCriteriaTicketVerdict:
    return ModelDodCriteriaTicketVerdict(
        ticket_id=ticket_id, contract_path=path, verdict=verdict, detail=detail
    )


def _pins(text: str) -> ModelCriteriaPins | str | None:
    """The contract's criteria pins, None when it records none, or the reason it is unreadable."""
    try:
        raw = yaml.safe_load(text)
    except yaml.YAMLError:
        return "the contract is not valid YAML"
    if not isinstance(raw, dict):
        return "the contract is not a mapping"
    items = raw.get("dod_evidence")
    return extract_criteria_pins(raw, items if isinstance(items, list) else [])


async def _send(
    transport: ProtocolCriteriaGithubTransport, request: ModelGithubHttpRequest
) -> ModelGithubHttpResponse:
    try:
        return await transport.send(request)
    except GithubLandingTransportError as error:
        raise _UnreadableError(
            f"GitHub did not answer ({type(error).__name__})"
        ) from error


async def _live_transport() -> UrllibGithubLandingTransport:
    ref = contract_secret_ref(_CONTRACT_PATH, _GITHUB_SECRET)
    secret = await resolve_api_key_async(ref, env_var_fallback=ref)
    if secret is None:
        raise _UnreadableError(
            f"api_key_ref {ref!r} resolved to None; set it in the secret store"
        )
    return UrllibGithubLandingTransport(secret)


async def _linear_tracker() -> ProtocolCriteriaTracker:
    return await linear_project_tracker(_CONTRACT_PATH)


__all__ = ["HandlerDodCriteriaCheck"]
