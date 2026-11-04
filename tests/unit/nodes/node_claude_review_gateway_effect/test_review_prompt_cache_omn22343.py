# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-22343: the review request carries cache breakpoints in the documented order.

Provider rules asserted (platform.claude.com prompt-caching): render order tools,
system, messages; at most four ``cache_control`` breakpoints; a 1h entry precedes
any 5m entry; usage reports ``cache_read_input_tokens`` on a prefix hit.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest
import yaml

from omnimarket.nodes.node_claude_review_gateway_effect.handlers import (
    handler_claude_review_gateway as gateway,
)
from omnimarket.nodes.node_claude_review_gateway_effect.handlers.review_prompt_cache import (
    load_prompt_cache,
    parse_prompt_cache,
)
from omnimarket.nodes.node_claude_review_gateway_effect.models.model_review_prompt_cache import (
    ModelReviewPromptCache,
)
from omnimarket.nodes.node_llm_delegation_call_effect.handlers import (
    anthropic_messages,
)

pytestmark = pytest.mark.unit

_FIXTURES = Path(__file__).parent / "fixtures"
_NODE = Path(gateway.__file__).resolve().parents[1]
_MARKER = gateway.BLOCK_BOUNDARY_MARKER


def _payload(
    diff: str = "DIFF OF PR A", persona: str = "persona: security"
) -> dict[str, Any]:
    return {
        "max_tokens": 4096,
        "messages": [
            {
                "role": "system",
                "content": f"rubric and catalog\n{_MARKER}\nstanding rules of the repository",
            },
            {"role": "user", "content": f"{diff}\n{_MARKER}\n{persona}"},
        ],
        "response_format": {
            "type": "json_schema",
            "json_schema": {
                "name": "submit_verdict",
                "schema": {"type": "object", "properties": {}},
            },
        },
    }


def _body(
    payload: dict[str, Any], cache: ModelReviewPromptCache | None = None
) -> dict[str, Any]:
    request = gateway.translate_chat_request(
        payload,
        model="claude-sonnet-5-5",
        endpoint_url="https://api.example.invalid/v1/messages",
        secret_ref="cred_ref",
        timeout_seconds=30.0,
        prompt_cache=cache,
    )
    body = anthropic_messages.build_messages_payload(request)
    body.pop("temperature", None)
    return body


def _breakpoints(body: dict[str, Any]) -> list[str]:
    blocks = [*body["system"], *body["messages"][0]["content"]]
    return [b["cache_control"]["ttl"] for b in blocks if "cache_control" in b]


def test_request_matches_golden_fixture() -> None:
    """Stable blocks first, the diff cached next, the persona last and uncached."""
    golden = json.loads((_FIXTURES / "omn22343_request_golden.json").read_text("utf-8"))
    assert _body(_payload()) == golden


def test_order_is_system_then_cached_diff_then_uncached_persona() -> None:
    body = _body(_payload())
    # The provider renders tools, then system, then messages whatever the key
    # order; a breakpoint on the first system block covers the tools before it.
    assert "tools" in body
    assert body["tools"][0]["name"] == body["tool_choice"]["name"]
    assert [b["text"] for b in body["system"]] == [
        "rubric and catalog",
        "standing rules of the repository",
    ]
    user = body["messages"][0]["content"]
    assert [b["text"] for b in user] == ["DIFF OF PR A", "persona: security"]
    assert "cache_control" in user[0]
    assert "cache_control" not in user[-1]


def test_at_most_four_breakpoints_and_longer_ttl_first() -> None:
    ttls = _breakpoints(_body(_payload()))
    assert len(ttls) <= 4
    order = {"1h": 0, "5m": 1}
    assert ttls == sorted(ttls, key=order.__getitem__)


def test_ttl_comes_from_the_contract_not_the_handler() -> None:
    contract = yaml.safe_load((_NODE / "contract.yaml").read_text("utf-8"))
    declared = contract["config"]["prompt_cache"]
    assert load_prompt_cache().system_ttl == declared["system_ttl"]
    assert load_prompt_cache().diff_ttl == declared["diff_ttl"]
    source = (_NODE / "handlers" / "handler_claude_review_gateway.py").read_text(
        "utf-8"
    )
    assert not re.search(r"""cache_ttl\s*=\s*["']""", source)
    assert "_SYSTEM_CACHE_TTL" not in source
    assert "_DIFF_CACHE_TTL" not in source


def test_changed_policy_changes_the_request() -> None:
    changed = parse_prompt_cache(
        {"config": {"prompt_cache": {"system_ttl": "5m", "diff_ttl": None}}}
    )
    assert _breakpoints(_body(_payload(), changed)) == ["5m", "5m"]
    body = _body(_payload(), changed)
    assert "cache_control" not in body["messages"][0]["content"][0]


def test_max_breakpoints_is_honoured_and_never_above_four() -> None:
    one = parse_prompt_cache({"config": {"prompt_cache": {"max_breakpoints": 1}}})
    assert len(_breakpoints(_body(_payload(), one))) == 1
    with pytest.raises(ValueError, match="less than or equal to 4"):
        ModelReviewPromptCache(max_breakpoints=5)


def test_a_one_hour_diff_after_a_five_minute_system_is_refused() -> None:
    with pytest.raises(ValueError, match="longer TTLs"):
        ModelReviewPromptCache(system_ttl="5m", diff_ttl="1h")


def test_missing_policy_section_is_refused() -> None:
    with pytest.raises(ValueError, match=r"config\.prompt_cache"):
        parse_prompt_cache({"config": {}})


def test_second_request_with_identical_prefix_reads_the_cache() -> None:
    """Two requests whose cached prefix is byte-identical, the persona differing."""
    first = _body(_payload(persona="persona: security"))
    second = _body(_payload(persona="persona: correctness"))
    assert first["system"] == second["system"]
    assert first["messages"][0]["content"][0] == second["messages"][0]["content"][0]
    recorded = json.loads(
        (_FIXTURES / "omn22343_second_pass_usage.json").read_text("utf-8")
    )
    one = anthropic_messages.parse_messages_response(recorded["first_pass"])
    two = anthropic_messages.parse_messages_response(recorded["second_pass"])
    assert one is not None
    assert two is not None
    assert one.cache_read_input_tokens == 0
    assert one.cache_creation_input_tokens > 0
    assert two.cache_read_input_tokens > 0
    assert two.cache_creation_input_tokens == 0
