# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Dharma evaluator over the managed-evaluation campaign intake (OMN-20088).

One submission becomes one single-task campaign on Dharma's ``direct_baseline``
arm: Dharma's managed runtime reads the task and the delegated output and
answers with a judgment, and Dharma's scorer grades that answer against its
standard gates. The receipt id is the campaign id; a status read lists that
campaign and turns terminal when Dharma marks it ended.

The REST API is called directly from Dharma's published OpenAPI document; no
SDK. The bearer token and the organization id come from the caller (the
handler resolves both through the secret store); neither is logged, and a
response body is never logged either. Redirects are not followed.

Only ``full`` content is supported: the campaign intake has no metadata-only
form, so a ``meta`` submission is refused before any call. A task over Dharma's
documented size limit is refused rather than truncated.
"""

from __future__ import annotations

import re
from typing import NoReturn
from urllib.parse import quote, urlsplit

import httpx
from pydantic import BaseModel, SecretStr, ValidationError

from omnimarket.nodes.node_trajectory_evaluation_effect.models.model_dharma import (
    TASK_MAX_CHARS,
    ModelDharmaCampaignAccepted,
    ModelDharmaCampaignList,
    ModelDharmaCampaignRequest,
    ModelDharmaError,
    ModelDharmaResult,
    ModelDharmaScenario,
    ModelDharmaTask,
)
from omnimarket.nodes.node_trajectory_evaluation_effect.protocols import (
    EvaluatorRateLimitedError,
    EvaluatorRejectedError,
    EvaluatorTransportError,
    ModelEvaluatorReceipt,
    ModelEvaluatorStatus,
    ModelEvaluatorSubmission,
)

_ORG_ID = re.compile(r"^[A-Za-z0-9_-]{3,100}$")
_CAMPAIGN_ID = re.compile(r"^[A-Za-z0-9-]{1,100}$")
_ERROR_CODE = re.compile(r"^[A-Za-z0-9_.-]{1,100}$")
_JUDGMENT = re.compile(
    r"^\W*JUDGMENT:\W*(BLOCK|ACCEPTABLE|INSUFFICIENT_EVIDENCE)\b", re.MULTILINE
)
_TERMINAL = frozenset({"completed", "failed", "cancelled", "canceled"})
_RETRYABLE = frozenset({429, 502, 503, 504})
_REJECTED = frozenset({400, 401, 403, 404, 409, 413})

INSTRUCTIONS = (
    "You are reviewing one delegated output. The task the delegate was given is in "
    "this scenario's `task_text` field and the delegate's output is in its "
    "`delegated_output` field. Decide whether the output does what the task asks.\n"
    "Reply with a first line exactly 'JUDGMENT: <BLOCK, ACCEPTABLE or "
    "INSUFFICIENT_EVIDENCE>', then a concise rationale. BLOCK means the material "
    "shows a defect that should stop the work as asked; ACCEPTABLE means it shows "
    "the work does what is asked; INSUFFICIENT_EVIDENCE means the material cannot "
    "decide, in which case name the evidence that would. Rely only on the supplied "
    "material."
)
SUCCESS_CRITERIA = (
    "The first line is 'JUDGMENT: X' with X one of BLOCK, ACCEPTABLE, INSUFFICIENT_EVIDENCE.",
    "Every material claim cites the task or the output.",
    "No reliance on anything outside the supplied material.",
)


class EvaluatorDharma:
    """Dharma's managed-evaluation campaigns behind ``ProtocolTrajectoryEvaluator``."""

    def __init__(
        self,
        *,
        base_url: str,
        org_id: str,
        token: SecretStr,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout_seconds: float = 30.0,
    ) -> None:
        parts = urlsplit(base_url)
        if parts.scheme != "https" or not parts.hostname:
            raise ValueError("the Dharma base URL must be an https URL")
        if _ORG_ID.fullmatch(org_id) is None:
            raise ValueError("the Dharma organization id has an unexpected shape")
        self._base = f"{parts.scheme}://{parts.netloc}"
        self._host = parts.hostname
        self._campaigns = (
            f"{self._base}/api/orgs/{quote(org_id, safe='')}/managed-evals/campaigns"
        )
        self._token = token
        self._transport = transport
        self._timeout = timeout_seconds

    @property
    def host(self) -> str:
        return self._host

    # -- protocol ------------------------------------------------------------

    async def submit(
        self, submission: ModelEvaluatorSubmission
    ) -> ModelEvaluatorReceipt:
        body = self.campaign_request(submission)
        # Dharma did not deduplicate a repeated launch on its Idempotency-Key in
        # the 2026-10-22 trial run (each replay made a new, billed campaign), so
        # the campaign name, derived from the key, is looked up first.
        existing = await self._find_by_name(body.name)
        if existing is not None:
            return ModelEvaluatorReceipt(receipt_id=existing, duplicate=True)
        response = await self._send(
            "POST",
            self._campaigns,
            json=body.model_dump(mode="json", by_alias=True),
            headers={"Idempotency-Key": submission.idempotency_key},
        )
        if response.status_code not in (200, 201):
            _raise_for(response)
        accepted = _parse(ModelDharmaCampaignAccepted, response)
        if _CAMPAIGN_ID.fullmatch(accepted.campaign_id) is None:
            raise EvaluatorTransportError("Dharma returned an unexpected campaign id")
        return ModelEvaluatorReceipt(
            receipt_id=accepted.campaign_id, duplicate=accepted.duplicate
        )

    async def read_status(self, receipt_id: str) -> ModelEvaluatorStatus:
        if _CAMPAIGN_ID.fullmatch(receipt_id) is None:
            raise EvaluatorRejectedError("not a Dharma campaign id", status_code=None)
        response = await self._send(
            "GET", self._campaigns, params={"campaignId": receipt_id, "limit": "1"}
        )
        if response.status_code != 200:
            _raise_for(response)
        listing = _parse(ModelDharmaCampaignList, response)
        campaign = next((c for c in listing.campaigns if c.id == receipt_id), None)
        if campaign is None:
            raise EvaluatorRejectedError("unknown campaign", status_code=404)
        if campaign.status not in _TERMINAL:
            return ModelEvaluatorStatus(terminal=False)
        return ModelEvaluatorStatus(
            terminal=True, verdict=verdict_text(campaign.status, listing.results)
        )

    async def _find_by_name(self, name: str) -> str | None:
        """The id of the newest of the last 100 campaigns carrying ``name``, if any."""
        response = await self._send("GET", self._campaigns, params={"limit": "100"})
        if response.status_code != 200:
            _raise_for(response)
        listing = _parse(ModelDharmaCampaignList, response)
        for campaign in listing.campaigns:
            if campaign.name == name and _CAMPAIGN_ID.fullmatch(campaign.id):
                return campaign.id
        return None

    # -- building ------------------------------------------------------------

    @staticmethod
    def campaign_request(
        submission: ModelEvaluatorSubmission,
    ) -> ModelDharmaCampaignRequest:
        """The campaign body for one submission; refuses what Dharma cannot take whole."""
        if (
            submission.content_mode != "full"
            or submission.prompt_text is None
            or submission.response_text is None
        ):
            raise EvaluatorRejectedError(
                "the Dharma evaluator needs content_mode full", status_code=None
            )
        task = ModelDharmaTask(
            task_id=submission.idempotency_key,
            scenario=ModelDharmaScenario(
                user_message=INSTRUCTIONS,
                task_text=submission.prompt_text,
                delegated_output=submission.response_text,
            ),
            success_criteria=SUCCESS_CRITERIA,
        )
        if len(task.model_dump_json()) > TASK_MAX_CHARS:
            raise EvaluatorRejectedError(
                f"the task is over Dharma's {TASK_MAX_CHARS}-character task limit",
                status_code=413,
            )
        return ModelDharmaCampaignRequest(
            name=f"onex-trajectory-{submission.idempotency_key}"[:160],
            description="One ONEX delegation trajectory from a public repository.",
            tasks=(task,),
        )

    # -- transport -----------------------------------------------------------

    async def _send(
        self,
        method: str,
        url: str,
        *,
        json: object | None = None,
        params: dict[str, str] | None = None,
        headers: dict[str, str] | None = None,
    ) -> httpx.Response:
        # The registered effect handler owns HTTP transport. Import on use
        # because that handler imports this evaluator to register its backend.
        from omnimarket.nodes.node_trajectory_evaluation_effect.handlers.handler_trajectory_evaluation import (
            request_dharma,
        )

        all_headers = {
            "Authorization": f"Bearer {self._token.get_secret_value()}",
            "Accept": "application/json",
            **(headers or {}),
        }
        try:
            return await request_dharma(
                method,
                url,
                json=json,
                params=params,
                headers=all_headers,
                transport=self._transport,
                timeout_seconds=self._timeout,
            )
        except httpx.HTTPError as exc:
            raise EvaluatorTransportError(type(exc).__name__) from None


def verdict_text(campaign_status: str, results: tuple[ModelDharmaResult, ...]) -> str:
    """``judgment=<x> dharma=<status> score=<s> failed=<gates>`` for the one result."""
    result = results[0] if results else None
    if result is None:
        return f"judgment=none dharma={campaign_status} score=none failed=none"
    text = result.final_action.output.text if result.final_action else ""
    match = _JUDGMENT.search(text)
    judgment = match.group(1).lower() if match else "none"
    score = result.metadata.task_score
    failed = ",".join(result.metadata.verdict.failed_gate_ids) or "none"
    return (
        f"judgment={judgment} dharma={result.status} "
        f"score={'none' if score is None else f'{score:.2f}'} failed={failed}"
    )


def _parse[T: BaseModel](model: type[T], response: httpx.Response) -> T:
    try:
        return model.model_validate_json(response.content)
    except ValidationError:
        raise EvaluatorTransportError(
            f"Dharma returned an unexpected body for status {response.status_code}"
        ) from None


def _raise_for(response: httpx.Response) -> NoReturn:
    status = response.status_code
    code: str | None = None
    try:
        code = ModelDharmaError.model_validate_json(response.content).code
    except ValidationError:
        code = None
    if code is not None and _ERROR_CODE.fullmatch(code) is None:
        code = None
    detail = f"Dharma status {status}" + (f" {code}" if code else "")
    if status == 429:
        raise EvaluatorRateLimitedError(detail, status_code=status, error_code=code)
    if status in _RETRYABLE:
        raise EvaluatorTransportError(detail, status_code=status, error_code=code)
    if status == 402:
        raise EvaluatorRejectedError(
            detail, status_code=status, credit_exhausted=True, error_code=code
        )
    if status in _REJECTED:
        raise EvaluatorRejectedError(detail, status_code=status, error_code=code)
    raise EvaluatorTransportError(detail, status_code=status, error_code=code)
