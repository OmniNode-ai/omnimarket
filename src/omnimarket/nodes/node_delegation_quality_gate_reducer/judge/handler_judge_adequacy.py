# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""LLM-judge adequacy scorer for the delegation quality gate (OMN-13470).

Drop-in judge handler. It does NOT introduce a new node: the judge LLM call is an
EFFECT performed through the canonical inference bridge
(``omnimarket.inference.adapter_inference_bridge.AdapterInferenceBridge`` — the
same surface the reviewer/grader effect nodes use), never an inline model call
inside the deterministic gate reducer.

Flow:
  1. Build a rubric-grounded adequacy prompt over (task, candidate output,
     acceptance criteria).
  2. Run it through the inference effect (model + endpoint resolved from the
     routing contract + overlay via the bridge config / model_key).
  3. Parse a 0.0-1.0 adequacy score and a verdict.
  4. Build a controlled, reproducible ``ModelDelegationJudgeVerdictEvent`` so the
     verdict is captured to the ``delegation-judge-verdict.v1`` projection and
     read back on replay (the recorded reasoning/score is reused, the model is
     never re-called).

Replay-safety: this handler does no I/O of its own — it delegates the single LLM
call to the injected ``ModelInferenceAdapter``. On replay the harness supplies an
adapter that returns the recorded judge response, so the verdict is reconstructed
without a live model call.

Fail-closed: a judge call failure or an unparseable response yields a
``JUDGE_FAILED`` verdict with ``actual_score=None``; the gate then proceeds on the
deterministic score alone (no silent zero, no silent pass).
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any
from uuid import UUID

import yaml

from omnimarket.events.delegation_judge_verdict import (
    EnumDelegationJudgeVerdict,
    ModelDelegationJudgeVerdictEvent,
    build_delegation_judge_verdict_event,
)
from omnimarket.inference.adapter_inference_bridge import (
    AdapterInferenceBridge,
    ModelInferenceAdapter,
    ModelInferenceBridgeConfig,
)

logger = logging.getLogger(__name__)

_RUBRIC_PATH = (
    Path(__file__).resolve().parent.parent.parent.parent
    / "configs"
    / "delegation_judge_rubrics.v1.yaml"
)

# Rubric that supplies semantic-adequacy authority for the verifiable code
# classes (OMN-13470). The deterministic check set stays the hard floor in the
# gate; this rubric is the supplemental adequacy band combined with it.
_CODE_RUBRIC_ID = "delegation_code_adequacy_v1"

# Default judge model_key. The concrete endpoint/model/key are resolved by the
# inference bridge from the routing contract + overlay (model_configs), never an
# env var in this module.
_DEFAULT_JUDGE_MODEL_KEY = "cheap_cloud"
_DEFAULT_JUDGE_TIMEOUT_SECONDS = 60.0

_SYSTEM_PROMPT = (
    "You are a strict semantic-adequacy judge for delegated AI work.\n"
    "\n"
    "You score whether a candidate answer ADEQUATELY fulfills the requested task. "
    "This is a semantic judgement, not a style or length judgement: a short but "
    "correct answer is adequate; a long but wrong, refusing, or evasive answer is "
    "not.\n"
    "\n"
    "A refusal, an apology with no answer, an empty answer, or a placeholder/stub "
    "is INADEQUATE and must score at most 0.2.\n"
    "\n"
    "Respond with ONLY a JSON object, no prose:\n"
    '{"adequacy_score": <float 0.0-1.0>, "reasoning": "<one short sentence>"}\n'
)

_JSON_OBJECT_RE = re.compile(r"\{.*\}", re.DOTALL)


def _load_rubric(rubric_id: str) -> dict[str, Any]:
    """Load one rubric definition from the contract config. Fail fast if absent."""
    raw_rubric = _RUBRIC_PATH.read_text()  # node-purity-ok: judge effect rubric
    data = yaml.safe_load(raw_rubric)
    rubrics = data["rubrics"]
    if rubric_id not in rubrics:
        raise KeyError(
            f"Rubric {rubric_id!r} not declared in {_RUBRIC_PATH}; "
            "declare it before scoring."
        )
    return dict(rubrics[rubric_id])


def _build_user_prompt(
    *,
    task_type: str,
    prompt: str,
    candidate_output: str,
    acceptance_criteria: tuple[str, ...],
) -> str:
    """Build the adequacy-rating prompt over (task, candidate, acceptance criteria)."""
    criteria_block = (
        "\n".join(f"- {c}" for c in acceptance_criteria)
        if acceptance_criteria
        else "(none declared; judge against the task request)"
    )
    return (
        f"## Task type\n{task_type}\n\n"
        f"## Requested task\n{prompt}\n\n"
        f"## Acceptance criteria\n{criteria_block}\n\n"
        f"## Candidate answer\n{candidate_output}\n\n"
        "Score how adequately the candidate answer fulfills the requested task."
    )


def _parse_adequacy(raw: str) -> tuple[float, str] | None:
    """Parse {adequacy_score, reasoning} JSON. Returns None on parse failure."""
    match = _JSON_OBJECT_RE.search(raw)
    if not match:
        return None
    try:
        data = json.loads(match.group())
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict) or "adequacy_score" not in data:
        return None
    raw_score = data.get("adequacy_score")
    if isinstance(raw_score, bool):
        return None
    try:
        score = float(raw_score)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if not (0.0 <= score <= 1.0):
        return None
    reasoning = str(data.get("reasoning", "No reasoning provided."))
    return score, reasoning


def _verdict_for_score(
    score: float, rubric: dict[str, Any]
) -> EnumDelegationJudgeVerdict:
    """Map a score to a verdict using the rubric thresholds."""
    thresholds = rubric["verdict_thresholds"]
    if score >= float(thresholds["pass_min_score"]):
        return EnumDelegationJudgeVerdict.PASS
    if score >= float(thresholds["borderline_min_score"]):
        return EnumDelegationJudgeVerdict.BORDERLINE
    return EnumDelegationJudgeVerdict.FAIL


class HandlerJudgeAdequacy:
    """EFFECT: score candidate adequacy via the canonical inference bridge.

    Inject ``inference_bridge`` in tests/replay to avoid (or replay) the network
    call. The judge model/endpoint/key are resolved by the bridge from the
    routing contract + overlay (``model_configs``); only the literal API key lives
    in the secret store, resolved at the bridge effect boundary.
    """

    def __init__(
        self,
        inference_bridge: ModelInferenceAdapter | None = None,
        judge_model_key: str = _DEFAULT_JUDGE_MODEL_KEY,
        judge_timeout_seconds: float = _DEFAULT_JUDGE_TIMEOUT_SECONDS,
        rubric_id: str = _CODE_RUBRIC_ID,
    ) -> None:
        self._bridge: ModelInferenceAdapter = (
            inference_bridge or AdapterInferenceBridge(ModelInferenceBridgeConfig())
        )
        self._judge_model_key = judge_model_key
        self._judge_timeout_seconds = judge_timeout_seconds
        self._rubric_id = rubric_id

    async def score(
        self,
        *,
        correlation_id: UUID,
        task_type: str,
        prompt: str,
        candidate_output: str,
        acceptance_criteria: tuple[str, ...] = (),
        judge_provider: str = "routing-contract",
    ) -> ModelDelegationJudgeVerdictEvent:
        """Score candidate adequacy and return a durable judge verdict event."""
        rubric = _load_rubric(self._rubric_id)
        temperature = float(rubric["temperature"])
        judge_model_version = str(rubric["judge_model_version"])
        judge_node_version = str(rubric["judge_node_version"])
        rubric_hash = str(rubric["rubric_hash"])
        user_prompt = _build_user_prompt(
            task_type=task_type,
            prompt=prompt,
            candidate_output=candidate_output,
            acceptance_criteria=acceptance_criteria,
        )

        try:
            raw = await self._bridge.infer(
                model_key=self._judge_model_key,
                system_prompt=_SYSTEM_PROMPT,
                user_prompt=user_prompt,
                timeout_seconds=self._judge_timeout_seconds,
                temperature=temperature,
            )
        except Exception as exc:
            logger.warning(
                "judge-adequacy LLM call failed (task_type=%s, cid=%s): %s",
                task_type,
                correlation_id,
                exc,
            )
            return build_delegation_judge_verdict_event(
                correlation_id=correlation_id,
                task_type=task_type,
                judge_model=self._judge_model_key,
                judge_model_version=judge_model_version,
                judge_provider=judge_provider,
                rubric_id=self._rubric_id,
                rubric_hash=rubric_hash,
                prompt=user_prompt,
                judged_input=candidate_output,
                temperature=temperature,
                judge_node_version=judge_node_version,
                reasoning=f"{type(exc).__name__}: {exc}",
                verdict=EnumDelegationJudgeVerdict.JUDGE_FAILED,
                actual_score=None,
                failure_kind="JUDGE_LLM_CALL_FAILED",
                failure_message=f"{type(exc).__name__}: {exc}",
            )

        parsed = _parse_adequacy(raw)
        if parsed is None:
            logger.warning(
                "judge-adequacy parse failed (task_type=%s, raw_len=%d)",
                task_type,
                len(raw),
            )
            return build_delegation_judge_verdict_event(
                correlation_id=correlation_id,
                task_type=task_type,
                judge_model=self._judge_model_key,
                judge_model_version=judge_model_version,
                judge_provider=judge_provider,
                rubric_id=self._rubric_id,
                rubric_hash=rubric_hash,
                prompt=user_prompt,
                judged_input=candidate_output,
                temperature=temperature,
                judge_node_version=judge_node_version,
                reasoning="unparseable judge response",
                verdict=EnumDelegationJudgeVerdict.JUDGE_FAILED,
                actual_score=None,
                failure_kind="JUDGE_PARSE_FAILED",
                failure_message=f"Could not parse adequacy score from response (len={len(raw)})",
            )

        score, reasoning = parsed
        verdict = _verdict_for_score(score, rubric)
        return build_delegation_judge_verdict_event(
            correlation_id=correlation_id,
            task_type=task_type,
            judge_model=self._judge_model_key,
            judge_model_version=judge_model_version,
            judge_provider=judge_provider,
            rubric_id=self._rubric_id,
            rubric_hash=rubric_hash,
            prompt=user_prompt,
            judged_input=candidate_output,
            temperature=temperature,
            judge_node_version=judge_node_version,
            reasoning=reasoning,
            verdict=verdict,
            actual_score=score,
        )


__all__ = ["HandlerJudgeAdequacy"]
