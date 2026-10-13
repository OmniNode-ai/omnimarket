# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The Claude review key budget: the fill-first key that keeps its reserve, or none.

Prices are the pricing manifest's claude-sonnet-5-5 row ($2 input, $10 output
per million tokens); the allowance (200 USD), reserve (10 USD), cycle and cache
multiples are the node contract's ``budget`` block. No key value appears here:
a key is named by a reference.
"""

from __future__ import annotations

import inspect
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest
import yaml
from pydantic import ValidationError

from omnimarket.enums.enum_claude_review_budget import EnumClaudeReviewBudgetReason
from omnimarket.models.claude_review_budget.claude_review_budget_math import (
    cycle_start,
)
from omnimarket.models.claude_review_budget.model_claude_review_usage import (
    ModelClaudeReviewUsage,
)
from omnimarket.nodes.node_claude_review_budget_compute import (
    HandlerClaudeReviewBudget,
    NodeClaudeReviewBudgetCompute,
)
from omnimarket.nodes.node_claude_review_budget_compute.handlers import (
    handler_claude_review_budget,
)
from omnimarket.nodes.node_claude_review_budget_compute.handlers.handler_claude_review_budget import (
    load_budget_config,
)
from omnimarket.nodes.node_claude_review_budget_compute.models import (
    ModelClaudeReviewBudgetKey,
    ModelClaudeReviewBudgetRequest,
    ModelClaudeReviewBudgetResult,
)

pytestmark = pytest.mark.unit

NODE = (
    Path(__file__).resolve().parents[4]
    / "src/omnimarket/nodes/node_claude_review_budget_compute"
)
MODEL = "claude-sonnet-5-5"
ANCHOR = datetime(2026, 9, 15, 0, 0, tzinfo=UTC)
NOW = datetime(2026, 10, 12, 7, 0, tzinfo=UTC)
# One review as the gateway sends it: a 40,000-token prompt, max_tokens 4,096.
PROMPT, MAX_TOKENS = 40_000, 4_096
WORST = Decimal("0.12096")  # 40,000 x $2/MTok + 4,096 x $10/MTok


def _call(
    ref: str,
    *,
    at: datetime = NOW - timedelta(days=1),
    input_tokens: int = 0,
    cache_write: int = 0,
    cache_read: int = 0,
    output_tokens: int = 0,
    model: str = MODEL,
) -> ModelClaudeReviewUsage:
    return ModelClaudeReviewUsage(
        call_id=uuid4(),
        api_key_ref=ref,
        model_id=model,
        observed_at=at,
        input_tokens=input_tokens,
        cache_creation_input_tokens=cache_write,
        cache_read_input_tokens=cache_read,
        output_tokens=output_tokens,
    )


def _spent(
    ref: str, usd: Decimal, *, at: datetime = NOW - timedelta(days=1)
) -> ModelClaudeReviewUsage:
    """A call that spent exactly ``usd`` in output tokens ($10 per million)."""
    tokens = usd * 100_000
    assert tokens == int(tokens)
    return _call(ref, at=at, output_tokens=int(tokens))


def _key(
    ref: str, *calls: ModelClaudeReviewUsage, anchor: datetime = ANCHOR
) -> ModelClaudeReviewBudgetKey:
    return ModelClaudeReviewBudgetKey(
        api_key_ref=ref, cycle_anchor_at=anchor, calls=calls
    )


def _decide(
    *keys: ModelClaudeReviewBudgetKey,
    as_of: datetime = NOW,
    handler: HandlerClaudeReviewBudget | None = None,
) -> ModelClaudeReviewBudgetResult:
    return (handler or HandlerClaudeReviewBudget()).handle(
        ModelClaudeReviewBudgetRequest(
            as_of=as_of, prompt_tokens=PROMPT, max_tokens=MAX_TOKENS, keys=keys
        )
    )


# --- the floor (AC4's decision half) -----------------------------------------


def test_a_key_at_its_reserve_floor_returns_no_key() -> None:
    """190 USD spent of 200 leaves exactly the 10 USD reserve: no call fits."""
    result = _decide(_key("anthropic.key.a", _spent("anthropic.key.a", Decimal(190))))
    assert result.chosen_api_key_ref is None
    assert result.reason is EnumClaudeReviewBudgetReason.NO_KEY_ABOVE_RESERVE
    (key,) = result.keys
    assert key.remaining_usd == Decimal(10)
    assert key.affordable is False


def test_every_key_at_the_floor_returns_no_key() -> None:
    result = _decide(
        _key("anthropic.key.a", _spent("anthropic.key.a", Decimal(190))),
        _key("anthropic.key.b", _spent("anthropic.key.b", Decimal(195))),
    )
    assert result.chosen_api_key_ref is None
    assert result.reason is EnumClaudeReviewBudgetReason.NO_KEY_ABOVE_RESERVE


def test_a_call_that_lands_exactly_on_the_reserve_is_allowed() -> None:
    """remaining - worst == reserve still keeps the reserve; one more cent does not."""
    exact = Decimal(190) - WORST
    assert (
        _decide(
            _key(
                "anthropic.key.a",
                _call("anthropic.key.a", input_tokens=int(exact * 500_000)),
            )
        ).chosen_api_key_ref
        == "anthropic.key.a"
    )
    over = exact + Decimal("0.01")
    assert (
        _decide(
            _key(
                "anthropic.key.a",
                _call("anthropic.key.a", input_tokens=int(over * 500_000)),
            )
        ).chosen_api_key_ref
        is None
    )


def test_no_keys_returns_no_key() -> None:
    result = _decide()
    assert result.chosen_api_key_ref is None
    assert result.reason is EnumClaudeReviewBudgetReason.NO_KEYS


# --- fill-first -----------------------------------------------------------------


def test_fill_first_picks_the_lowest_index_key_that_can_afford_the_call() -> None:
    """Key a has less left than key b but can still afford it, so a serves."""
    result = _decide(
        _key("anthropic.key.a", _spent("anthropic.key.a", Decimal(150))),
        _key("anthropic.key.b"),
    )
    assert result.reason is EnumClaudeReviewBudgetReason.KEY_CHOSEN
    assert result.chosen_api_key_ref == "anthropic.key.a"


def test_fill_first_moves_on_only_when_the_key_cannot_afford_the_call() -> None:
    result = _decide(
        _key("anthropic.key.a", _spent("anthropic.key.a", Decimal(190))),
        _key("anthropic.key.b", _spent("anthropic.key.b", Decimal(195))),
        _key("anthropic.key.c", _spent("anthropic.key.c", Decimal(100))),
        _key("anthropic.key.d"),
    )
    assert result.chosen_api_key_ref == "anthropic.key.c"
    assert [k.affordable for k in result.keys] == [False, False, True, True]


# --- cycle rollover ---------------------------------------------------------------


def test_a_cycle_rollover_resets_the_spend() -> None:
    """190 USD spent in the cycle from 2026-09-15 does not count from 2026-10-15."""
    floor = _key(
        "anthropic.key.a",
        _spent("anthropic.key.a", Decimal(190), at=datetime(2026, 10, 1, tzinfo=UTC)),
    )
    before = _decide(floor, as_of=datetime(2026, 10, 14, 23, 59, tzinfo=UTC))
    assert before.chosen_api_key_ref is None
    assert before.keys[0].cycle_started_at == ANCHOR

    after = _decide(floor, as_of=datetime(2026, 10, 15, tzinfo=UTC))
    assert after.chosen_api_key_ref == "anthropic.key.a"
    assert after.keys[0].cycle_started_at == datetime(2026, 10, 15, tzinfo=UTC)
    assert after.keys[0].spent_usd == 0
    assert after.keys[0].calls_counted == 0


def test_a_call_after_as_of_is_not_counted() -> None:
    result = _decide(
        _key(
            "anthropic.key.a",
            _spent("anthropic.key.a", Decimal(190), at=NOW + timedelta(minutes=1)),
        )
    )
    assert result.chosen_api_key_ref == "anthropic.key.a"
    assert result.keys[0].calls_counted == 0


def test_cycle_start_clamps_to_the_end_of_a_short_month() -> None:
    anchor = datetime(2026, 1, 31, 12, tzinfo=UTC)
    assert cycle_start(anchor, datetime(2026, 2, 28, 12, tzinfo=UTC), 1) == datetime(
        2026, 2, 28, 12, tzinfo=UTC
    )
    assert cycle_start(anchor, datetime(2026, 3, 30, tzinfo=UTC), 1) == datetime(
        2026, 2, 28, 12, tzinfo=UTC
    )
    assert cycle_start(anchor, datetime(2026, 3, 31, 12, tzinfo=UTC), 1) == datetime(
        2026, 3, 31, 12, tzinfo=UTC
    )
    with pytest.raises(ValueError, match="before the cycle anchor"):
        cycle_start(anchor, anchor - timedelta(seconds=1), 1)


# --- Sonnet pricing arithmetic ----------------------------------------------------


def test_sonnet_worst_case_is_prompt_at_input_price_plus_max_tokens_at_output_price() -> (
    None
):
    result = _decide(_key("anthropic.key.a"))
    assert result.model_id == MODEL
    assert result.worst_case_usd == WORST


def test_sonnet_spend_prices_all_four_usage_counts() -> None:
    """One review: 40,000 input, 8,000 cache write, 20,000 cache read, 2,500 output.

    40,000 x $2/MTok = 0.08; 8,000 x $2.50/MTok = 0.02; 20,000 x $0.20/MTok =
    0.004; 2,500 x $10/MTok = 0.025; total 0.129 USD.
    """
    call = _call(
        "anthropic.key.a",
        input_tokens=40_000,
        cache_write=8_000,
        cache_read=20_000,
        output_tokens=2_500,
    )
    (key,) = _decide(_key("anthropic.key.a", call)).keys
    assert key.spent_usd == Decimal("0.129")
    assert key.remaining_usd == Decimal(200) - Decimal("0.129")


def test_a_model_the_manifest_does_not_price_chooses_no_key() -> None:
    result = _decide(
        _key("anthropic.key.a", _call("anthropic.key.a", model="claude-unpriced-0"))
    )
    assert result.chosen_api_key_ref is None
    assert result.reason is EnumClaudeReviewBudgetReason.MODEL_NOT_PRICED
    assert "claude-unpriced-0" in result.detail


# --- config comes from the contract ------------------------------------------------


def test_the_allowance_and_reserve_come_from_the_contract() -> None:
    config = load_budget_config()
    assert config.model_id == MODEL
    assert config.allowance_usd == Decimal(200)
    assert config.reserve_usd == Decimal(10)
    source = inspect.getsource(handler_claude_review_budget)
    for literal in ("200", "Decimal(10)", 'Decimal("10")', MODEL):
        assert literal not in source


def test_a_contract_with_a_larger_reserve_moves_the_floor() -> None:
    contract = yaml.safe_load((NODE / "contract.yaml").read_text())
    contract["budget"]["reserve_usd"] = "60"
    handler = HandlerClaudeReviewBudget(
        config=load_budget_config(yaml.safe_dump(contract))
    )
    key = _key("anthropic.key.a", _spent("anthropic.key.a", Decimal(150)))
    assert _decide(key).chosen_api_key_ref == "anthropic.key.a"
    assert _decide(key, handler=handler).chosen_api_key_ref is None


def test_a_reserve_at_or_above_the_allowance_is_refused() -> None:
    contract = yaml.safe_load((NODE / "contract.yaml").read_text())
    contract["budget"]["reserve_usd"] = "200"
    with pytest.raises(ValidationError):
        load_budget_config(yaml.safe_dump(contract))


# --- request validation and node shape ---------------------------------------------


def test_a_call_of_another_key_is_refused() -> None:
    with pytest.raises(ValidationError):
        _key("anthropic.key.a", _call("anthropic.key.b"))


def test_a_key_reference_named_twice_is_refused() -> None:
    with pytest.raises(ValidationError):
        ModelClaudeReviewBudgetRequest(
            as_of=NOW,
            prompt_tokens=1,
            max_tokens=1,
            keys=(_key("anthropic.key.a"), _key("anthropic.key.a")),
        )


def test_the_node_wraps_the_handler_and_the_contract_declares_its_topics() -> None:
    assert issubclass(NodeClaudeReviewBudgetCompute, HandlerClaudeReviewBudget)
    contract = yaml.safe_load((NODE / "contract.yaml").read_text())
    dispatch = contract["runtime_dispatch"]
    assert dispatch["command_topic"].startswith("onex.cmd.omnimarket.")
    assert set(dispatch["terminal_events"]) == {"success", "failure"}
    source = inspect.getsource(handler_claude_review_budget)
    assert "onex.evt." not in source
    assert "onex.cmd." not in source
