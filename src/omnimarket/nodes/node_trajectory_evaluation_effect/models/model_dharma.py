# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed wire models for Dharma's Evaluation Runs API (OMN-20088).

Shapes follow Dharma's published Evaluation Runs OpenAPI 3.1 document, version
1.1.0 (``/api/v1/evaluation-runs/openapi.json``), and the responses recorded from
one real trial-organization run on 2026-10-22 (``tests/fixtures/dharma/``).

The adapter uses the managed-evaluation campaign intake, not the evaluation
sidecar: the sidecar accepts only ``provider: gemini`` observations, and the
delegation chain's outputs come from other providers. Request models refuse
unknown fields so a typo never reaches the wire; response models ignore fields
they do not read, because Dharma documents ``additionalProperties: true`` on
most of them.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

# Dharma's documented limits for one task (customer onboarding guide).
USER_MESSAGE_MAX_CHARS = 20_000
TASK_MAX_CHARS = 100_000


class _ModelRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", populate_by_name=True)


class _ModelResponse(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore", populate_by_name=True)


# -- request -----------------------------------------------------------------


class ModelDharmaScenario(_ModelRequest):
    """What the evaluated agent sees. Extra fields are visible to it."""

    user_message: str = Field(min_length=1, max_length=USER_MESSAGE_MAX_CHARS)
    task_text: str
    delegated_output: str


class ModelDharmaTask(_ModelRequest):
    task_id: str = Field(min_length=1, max_length=200)
    scenario: ModelDharmaScenario
    success_criteria: tuple[str, ...] = ()


class ModelDharmaCampaignRequest(_ModelRequest):
    """``CampaignRequest``: one campaign carries one delegation trajectory.

    Serialize with ``by_alias=True``: the wire names are Dharma's camelCase.
    """

    name: str = Field(min_length=3, max_length=160)
    description: str = Field(default="", max_length=2000)
    integration_mode: Literal["observe_existing_runtime"] = Field(
        default="observe_existing_runtime", alias="integrationMode"
    )
    arms: tuple[Literal["direct_baseline"], ...] = ("direct_baseline",)
    tasks: tuple[ModelDharmaTask, ...] = Field(min_length=1, max_length=1)


# -- responses ---------------------------------------------------------------


class ModelDharmaCampaignAccepted(_ModelResponse):
    """``CampaignAcceptedResponse`` (201)."""

    ok: Literal[True]
    campaign_id: str = Field(alias="campaignId")
    status: str
    duplicate: bool = False


class ModelDharmaCampaign(_ModelResponse):
    id: str
    status: str
    name: str = ""


class ModelDharmaTaskRecord(_ModelResponse):
    id: str
    external_task_id: str


class ModelDharmaOutput(_ModelResponse):
    text: str = ""


class ModelDharmaFinalAction(_ModelResponse):
    output: ModelDharmaOutput = ModelDharmaOutput()


class ModelDharmaVerdict(_ModelResponse):
    status: str | None = None
    failed_gate_ids: tuple[str, ...] = Field(default=(), alias="failedGateIds")


class ModelDharmaResultMetadata(_ModelResponse):
    task_score: float | None = Field(default=None, alias="taskScore")
    verdict: ModelDharmaVerdict = ModelDharmaVerdict()


class ModelDharmaResult(_ModelResponse):
    task_id: str
    run_id: str | None = None
    status: str
    final_action: ModelDharmaFinalAction | None = None
    metadata: ModelDharmaResultMetadata = ModelDharmaResultMetadata()


class ModelDharmaCampaignList(_ModelResponse):
    """``CampaignListResponse`` (200) for one ``campaignId``."""

    ok: Literal[True]
    campaigns: tuple[ModelDharmaCampaign, ...] = ()
    tasks: tuple[ModelDharmaTaskRecord, ...] = ()
    results: tuple[ModelDharmaResult, ...] = ()


class ModelDharmaErrorDetail(_ModelResponse):
    code: str | None = None
    message: str | None = None
    correlation_id: str | None = Field(default=None, alias="correlationId")


class ModelDharmaError(_ModelResponse):
    """Both published error shapes.

    The Evaluation Runs spec's ``Error`` is flat (``{error, message}``, with
    ``error`` a code string); the docs' envelope nests it
    (``{ok: false, error: {code, message, correlationId}}``).
    """

    error: str | ModelDharmaErrorDetail | None = None
    message: str | None = None
    correlation_id: str | None = Field(default=None, alias="correlationId")

    @property
    def code(self) -> str | None:
        if isinstance(self.error, ModelDharmaErrorDetail):
            return self.error.code
        return self.error
