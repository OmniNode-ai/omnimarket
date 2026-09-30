# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-19432: the inference boundary's per-call ceiling is per model, and it is measured.

The effect boundary clamps every provider call to the contract's ``http_request``
``timeout_seconds`` (120 s, OMN-18852) so one slow rung cannot hold the lane's
inference slot past the caller's budget. That single number was sized for local
Qwen. Measured 2026-09-30 on the 30-task GLM lineup set with the documented
settings (thinking on, temperature 1.0, top_p 0.95), 60 answers a config:

============  =========  =========  =========  =========
model          <= 120 s   <= 150 s   <= 180 s   <= 240 s
============  =========  =========  =========  =========
glm-5.3        53 / 60    54 / 60    56 / 60    57 / 60
glm-5.3-flash  52 / 60    55 / 60    57 / 60    57 / 60
============  =========  =========  =========  =========

So 7 of 60 glm-5.3 answers were failed closed as TIMEOUT at 120 s, and the ladder
paid for another rung to re-ask a question that was 30 s from answered. The rung
that takes over from flash needs a longer allowance than the flash rung, but not
an unbounded one: flash keeps 120 s, glm-5.3 gets 150 s, and the two together still
fit inside the delegate-skill caller's whole wait with room for the gate.

The allowance is declared in the effect node's own contract, keyed by the model id
the intent names, and the boundary reads it there. It is not read from the request:
the wire's ``timeout_seconds`` is producer-supplied on a public command topic, and a
bound that honours the request bounds nothing (OMN-18852).
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest
import yaml
from omnibase_core.models.delegation.wire import ModelInferenceIntent

from omnimarket.nodes.node_llm_delegation_call_effect.handlers.handler_inference_intent import (
    HandlerInferenceIntent,
)
from omnimarket.nodes.node_llm_delegation_call_effect.models.model_inference_call_budget import (
    load_inference_call_budget,
)

pytestmark = pytest.mark.unit

_ROOT = Path(__file__).resolve().parents[3] / "src" / "omnimarket" / "nodes"
_EFFECT_CONTRACT_PATH = _ROOT / "node_llm_delegation_call_effect" / "contract.yaml"
_DELEGATE_SKILL_CONTRACT_PATH = (
    _ROOT / "node_delegate_skill_orchestrator" / "contract.yaml"
)
_TASK_CLASS_CONTRACTS = (
    Path(__file__).resolve().parents[3]
    / "src"
    / "omnimarket"
    / "configs"
    / "task_class_contracts.v1.yaml"
)


def _intent(model: str, timeout_seconds: float = 300.0) -> ModelInferenceIntent:
    return ModelInferenceIntent(
        base_url="https://api.z.ai/api/coding/paas/v4/chat/completions",
        model=model,
        system_prompt="You are a helpful assistant.",
        prompt="Classify.",
        max_tokens=512,
        temperature=1.0,
        timeout_seconds=timeout_seconds,
        correlation_id=uuid4(),
    )


def _posted_timeout(model: str, timeout_seconds: float = 300.0) -> float:
    with patch("httpx.Client") as client_cls:  # onex-allow-faked-boundary
        client = MagicMock()
        client_cls.return_value.__enter__.return_value = client
        response = MagicMock()
        response.json.return_value = {
            "id": "x",
            "choices": [{"message": {"content": "ok"}}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        }
        response.raise_for_status.return_value = None
        client.post.return_value = response
        HandlerInferenceIntent().handle(_intent(model, timeout_seconds))
    return float(client.post.call_args.kwargs["timeout"])


def test_glm_5_3_is_allowed_longer_than_the_default_ceiling() -> None:
    budget = load_inference_call_budget()
    assert budget.ceiling_for("glm-5.3") == 150
    assert budget.ceiling_for("glm-5.3") > budget.max_inference_duration_seconds
    assert _posted_timeout("glm-5.3") == 150.0


def test_flash_and_every_undeclared_model_keep_the_default_ceiling() -> None:
    default = float(load_inference_call_budget().max_inference_duration_seconds)
    assert _posted_timeout("glm-5.3-flash") == default
    assert _posted_timeout("Qwen3.8-27B") == default
    assert _posted_timeout("gemini-2.5-flash") == default


def test_a_per_model_allowance_is_still_a_ceiling_never_a_floor() -> None:
    assert _posted_timeout("glm-5.3", timeout_seconds=20.0) == 20.0


def test_the_allowance_is_matched_on_the_model_id_the_intent_names() -> None:
    # A provider prefix does not smuggle a model past its ceiling or into another's.
    budget = load_inference_call_budget()
    assert budget.ceiling_for("zai/glm-5.3") == budget.ceiling_for("glm-5.3")
    assert budget.ceiling_for("glm-5.3-flashx") == budget.max_inference_duration_seconds


def test_the_contract_pins_flash_plus_glm_5_3_inside_the_callers_whole_wait() -> None:
    """Two rungs at their ceilings fit the caller's wait, with room for the gate.

    The caller (``handler_delegate_skill``) bounds the whole dispatch at the task
    class's execution ceiling plus the terminal delivery margin. flash at its
    ceiling, then glm-5.3 at its own, must leave at least 5 % of that wait for the
    quality gate and the terminal write, or the second rung is nominal.
    """
    budget = load_inference_call_budget()
    task_classes = yaml.safe_load(_TASK_CLASS_CONTRACTS.read_text(encoding="utf-8"))
    budgets = task_classes["execution_budgets"]
    for task_type, declared in budgets.items():
        whole_wait = (
            declared["task_class_timeout_ceiling_seconds"]
            + declared["terminal_delivery_margin_seconds"]
        )
        two_rungs = budget.max_inference_duration_seconds + budget.ceiling_for(
            "glm-5.3"
        )
        assert two_rungs <= 0.95 * whole_wait, (task_type, two_rungs, whole_wait)


def test_the_default_ceiling_still_leaves_room_for_a_second_rung() -> None:
    """The OMN-18852 invariant is unchanged for every model without an allowance."""
    raw = yaml.safe_load(_DELEGATE_SKILL_CONTRACT_PATH.read_text(encoding="utf-8"))
    caller_budget = int(raw["handler_execution_budget"]["max_handler_duration_seconds"])
    assert (
        2 * load_inference_call_budget().max_inference_duration_seconds <= caller_budget
    )


def _bad_contract(tmp_path: Path, model_timeouts: object) -> Path:
    raw = yaml.safe_load(_EFFECT_CONTRACT_PATH.read_text(encoding="utf-8"))
    for entry in raw["io_operations"]:
        if entry["operation_type"] == "http_request":
            entry["model_timeout_seconds"] = model_timeouts
    bad = tmp_path / "contract.yaml"
    bad.write_text(yaml.safe_dump(raw), encoding="utf-8")
    return bad


@pytest.mark.parametrize(
    ("model_timeouts", "match"),
    [
        ({"glm-5.3": 600}, "strictly less than"),
        ({"glm-5.3": 60}, "shorter than the default"),
        ({"glm-5.3": "long"}, "integer"),
        (["glm-5.3"], "mapping"),
    ],
)
def test_loader_refuses_a_per_model_allowance_that_cannot_bind_or_shortens(
    tmp_path: Path, model_timeouts: object, match: str
) -> None:
    with pytest.raises(ValueError, match=match):
        load_inference_call_budget(_bad_contract(tmp_path, model_timeouts))
