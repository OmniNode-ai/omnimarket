# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Review findings 3 to 5 of the local-tier input bound: meter, clamp, prompt, backend."""

from __future__ import annotations

import ast
import asyncio
import inspect
import random
import string
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

import pytest

from omnimarket.handlers.routing import input_token_bound as bound_module
from omnimarket.handlers.routing.input_token_bound import (
    LOCAL_MEMBER_INPUT_BOUNDS,
    LOCAL_MEMBER_OUTPUT_RESERVES,
    LOCAL_MEMBER_WINDOWS,
    WRAPPER_MARGIN_TOKENS,
    clamp_max_tokens,
    input_bound_members,
    input_token_upper_bound,
    member_input_bound,
)
from omnimarket.nodes.node_delegate_skill_orchestrator.ports import (
    port_local_delegation_dispatch as port_mod,
)
from omnimarket.nodes.node_delegation_routing_reducer.handlers import (
    handler_delegation_routing as routing,
)

from .test_local_rung_context_ceiling_omn24980 import (
    LOCAL_HEAVY,
    PEER,
    local_choices,
    packaged_ladder,
    with_spread_peer,
)

# The imported ``packaged_ladder`` is an autouse fixture; naming it here is what
# makes this module's tests run under it.
_AUTOUSE_FIXTURES = (packaged_ladder,)

pytestmark = pytest.mark.unit

RUNG = 64512
PEER_BOUND = 48128
SRC = Path(__file__).parents[3] / "src" / "omnimarket"


def hex_words(count: int, seed: int = 3) -> str:
    """Space-separated random 12-hex-character words, each holding a digit."""
    rng = random.Random(seed)
    words = []
    for _ in range(count):
        word = "".join(rng.choice("0123456789abcdef") for _ in range(12))
        words.append(word if any(c.isdigit() for c in word) else "0" + word[1:])
    return " ".join(words)


def mixed_case_words(letters: int, width: int = 12, seed: int = 11) -> str:
    rng = random.Random(seed)
    text = "".join(rng.choice(string.ascii_letters) for _ in range(letters))
    return " ".join(text[i : i + width] for i in range(0, letters, width))


class _Member:
    def __init__(self, backend_ref: str) -> None:
        self.backend_ref = backend_ref


# ---- finding 3: the meter, per sample, against the bound --------------------


def test_input_token_bound_meters_the_hex_word_sample_at_one_token_per_character() -> (
    None
):
    # About 6000 space-separated 12-hex-character words: 78000 characters, a meter
    # of up to one token per character. The old counting halved each (bound near
    # 64500, inside the rung's 64512) and admitted it.
    text = hex_words(6000)
    meter_at_one_token_per_character = len(text)
    assert meter_at_one_token_per_character > RUNG
    assert input_token_upper_bound(text) >= meter_at_one_token_per_character
    for ref in LOCAL_MEMBER_INPUT_BOUNDS:
        assert input_bound_members("local", [_Member(ref)], text) == ()


def test_input_token_bound_meters_the_96256_letter_sample_in_words_and_in_one_run() -> (
    None
):
    # The one-run form is refused on its length alone; the 12-letter word form
    # reads the case switches and must still be counted per letter.
    in_words = mixed_case_words(96256)
    letters = sum(c.isalpha() for c in in_words)
    assert letters == 96256
    assert input_token_upper_bound(in_words) >= letters
    one_run = in_words.replace(" ", "")
    assert input_token_upper_bound(one_run) >= 96256
    for text in (in_words, one_run):
        assert input_bound_members("local", [_Member("local-coder")], text) == ()


def test_input_token_bound_counts_overlapping_case_switches() -> None:
    # ``aBc`` holds two switches (aB, Bc); a non-overlapping findall counted one
    # and halved the run. 20000 such words meter 60000 letters (one token each)
    # plus 20000 separators; the old bound was 50000.
    assert input_token_upper_bound("aBc") >= 3
    text = " ".join(["aBc"] * 20000)
    assert input_token_upper_bound(text) >= len(text)  # one token per character
    assert input_bound_members("local", [_Member("local-coder")], text) == ()


def test_input_token_bound_still_admits_plain_prose_at_the_rung() -> None:
    # Control: natural words stay discounted, so this change does not refuse the
    # prompts the tier exists to take.
    prose = "the quick brown fox jumps over the lazy dog " * 2000  # 90000 chars
    assert input_token_upper_bound(prose) <= RUNG
    assert input_bound_members("local", [_Member("local-coder")], prose)


# ---- finding 3: max_tokens is clamped at dispatch ----------------------------


def test_clamp_max_tokens_cuts_the_rung_output_to_what_the_window_leaves() -> None:
    dense = hex_words(6000)  # bound 78000, window 131072
    room = 131072 - WRAPPER_MARGIN_TOKENS - input_token_upper_bound(dense)
    assert room < 65536
    assert clamp_max_tokens(dense, "local", "local-coder", 65536) == room
    # input + clamped output + margin never passes the window.
    assert (
        input_token_upper_bound(dense)
        + clamp_max_tokens(dense, "local", "local-coder", 65536)
        + WRAPPER_MARGIN_TOKENS
        <= 131072
    )


def test_clamp_max_tokens_leaves_an_admitted_prompt_and_other_tiers_alone() -> None:
    small = "summarise this" * 10
    assert clamp_max_tokens(small, "local", "local-coder", 65536) == 65536
    assert clamp_max_tokens(hex_words(6000), "cheap_frontier", "x", 65536) == 65536
    # No room left: refused, never sent with max_tokens 1 (finding 9).
    with pytest.raises(ValueError, match="no output room"):
        clamp_max_tokens(hex_words(60000), "local", "local-coder", 65536)


def test_clamp_max_tokens_uses_the_peer_window_not_the_rung_window() -> None:
    text = hex_words(3500)  # bound about 45500: inside the peer's 48128
    bound = input_token_upper_bound(text)
    assert bound < PEER_BOUND
    assert clamp_max_tokens(text, "local", "local-omnipc2-chat", 16384) == 16384
    over = hex_words(4000)  # bound about 52000: the peer's slot holds 65536
    assert clamp_max_tokens(over, "local", "local-omnipc2-chat", 16384) < 16384


def test_clamp_max_tokens_is_applied_to_the_resolved_backend_at_dispatch() -> None:
    from omnimarket.nodes.node_delegate_skill_orchestrator.ports import (
        port_local_delegation_dispatch as port,
    )

    source = inspect.getsource(port.LocalDelegationDispatchPort._run_single_attempt)
    assert "clamp_max_tokens(" in source
    assert "backend.tier" in source
    assert "backend.backend_id" in source


# ---- finding 4: prompt is required, on the gate and on every caller ---------


GATED = (
    "first_eligible_tier",
    "next_eligible_tier",
    "backend_id_for_tier",
    "sibling_backend_available_in_tier",
    "_tier_can_route_task",
)


@pytest.mark.parametrize("name", GATED)
def test_prompt_required_by_every_selection_function(name: str) -> None:
    parameter = inspect.signature(getattr(routing, name)).parameters["prompt"]
    assert parameter.default is inspect.Parameter.empty, name


def test_prompt_required_calls_without_it_fail() -> None:
    untyped_routing: Any = routing
    untyped_bound_members: Any = input_bound_members
    with pytest.raises(TypeError, match="prompt"):
        untyped_routing.first_eligible_tier("summarization")
    with pytest.raises(TypeError, match="prompt"):
        untyped_routing.next_eligible_tier(
            "local", frozenset(), task_type="summarization"
        )
    with pytest.raises(TypeError, match="prompt"):
        untyped_routing.backend_id_for_tier("local", "summarization")
    with pytest.raises(TypeError, match="prompt"):
        untyped_routing.sibling_backend_available_in_tier(
            *("local", "summarization", frozenset())
        )
    with pytest.raises(TypeError, match="prompt"):
        untyped_bound_members(*("local", [_Member("local-coder")]))


def _calls_without_prompt() -> list[str]:
    missing: list[str] = []
    for path in SRC.rglob("*.py"):
        if "input_token_bound" in path.name:
            continue
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = (
                func.id
                if isinstance(func, ast.Name)
                else func.attr
                if isinstance(func, ast.Attribute)
                else None
            )
            if name not in (*GATED, "input_bound_members"):
                continue
            has_prompt = any(k.arg == "prompt" for k in node.keywords) or any(
                k.arg is None for k in node.keywords
            )
            positional = {
                "input_bound_members": 3,
                "sibling_backend_available_in_tier": 4,
                "_tier_can_route_task": 5,
            }.get(name)
            if positional is not None and len(node.args) >= positional:
                has_prompt = True
            if not has_prompt:
                missing.append(f"{path.relative_to(SRC)}:{node.lineno} {name}")
    return missing


def test_prompt_required_every_caller_in_src_passes_it() -> None:
    assert _calls_without_prompt() == []


def test_prompt_required_second_pass_after_the_roi_suppression_passes_it() -> None:
    # The fail-safe second pass is the second iteration of the one call inside
    # ``for skip in (suppressed, frozenset())``; that call takes the prompt, so
    # an over-bound prompt is refused on the second pass too.
    tree = ast.parse(inspect.getsource(routing.first_eligible_tier))
    calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "_tier_can_route_task"
    ]
    assert len(calls) == 1
    assert len(calls[0].args) >= 5 or any(k.arg == "prompt" for k in calls[0].keywords)
    small = routing.first_eligible_tier("summarization", prompt="summarise this")
    assert small == "local"
    over = routing.first_eligible_tier("summarization", prompt=hex_words(6000))
    assert over != "local"


def test_prompt_required_the_production_dispatch_path_carries_the_prompt() -> None:
    # probe_ladder is None on every production run. The routing resolutions of the
    # run read the prompt from the active-prompt context; it must be the run's
    # prompt on that path, not the empty default (which keeps every local member).
    seen: list[str] = []

    class _Capture(port_mod.LocalDelegationDispatchPort):
        async def _dispatch_rungs(self, **kwargs: Any) -> dict[str, object]:
            seen.append(port_mod._ACTIVE_PROMPT.get())
            return {}

    text = hex_words(6000)
    asyncio.run(
        object.__new__(_Capture).dispatch(
            prompt=text,
            task_type="summarization",
            correlation_id=uuid4(),
            max_tokens=None,
            source_file_path=None,
            source_session_id=None,
            wait=False,
            execution_timeout_seconds=60,
            terminal_delivery_margin_seconds=5,
            quality_contract_mode="standard",
            acceptance_criteria=(),
            tenant_id=None,
        )
    )
    assert seen == [text]
    with pytest.raises(LookupError):
        port_mod._ACTIVE_PROMPT.get()


# ---- finding 5: the bound comes from the resolved backend -------------------


def test_resolved_backend_bound_is_window_less_reserve_less_margin() -> None:
    assert set(LOCAL_MEMBER_WINDOWS) == set(LOCAL_MEMBER_OUTPUT_RESERVES)
    for ref, window in LOCAL_MEMBER_WINDOWS.items():
        assert (
            LOCAL_MEMBER_INPUT_BOUNDS[ref]
            == window - LOCAL_MEMBER_OUTPUT_RESERVES[ref] - WRAPPER_MARGIN_TOKENS
        )
    assert LOCAL_MEMBER_INPUT_BOUNDS["local-coder"] == RUNG
    assert LOCAL_MEMBER_INPUT_BOUNDS["local-omnipc2-chat"] == PEER_BOUND
    assert PEER["window_per_slot_tokens"] == LOCAL_MEMBER_WINDOWS["local-omnipc2-chat"]
    assert PEER["max_tokens"] == LOCAL_MEMBER_OUTPUT_RESERVES["local-omnipc2-chat"]


def test_resolved_backend_max_tokens_moves_the_bound() -> None:
    # A peer resolved with a larger output reserve than the table declares takes
    # a smaller input, and a rung resolved with a smaller one a larger input.
    assert member_input_bound("local", "local-omnipc2-chat", 16384) == PEER_BOUND
    assert member_input_bound("local", "local-omnipc2-chat", 32768) == 31744
    assert member_input_bound("local", "local-coder", 65536) == RUNG
    assert member_input_bound("local", "local-coder", 32768) == 97280
    assert member_input_bound("cheap_frontier", "x", 1) is None
    assert member_input_bound("local", "unlisted-peer") == PEER_BOUND


def test_resolved_backend_gate_uses_member_input_bound() -> None:
    assert "member_input_bound" in inspect.getsource(bound_module.input_bound_members)


@pytest.mark.parametrize("words", [3800, 4300, 4900])
def test_placement_spread_never_substitutes_the_peer_above_its_bound(
    words: int, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # A prompt bounded between the peer's 48128 and the rungs' 64512 goes through
    # the loaded spread placement (the peer is a first-choice peer of the rung)
    # over 200 correlation ids: the peer is never the member.
    text = hex_words(words)
    assert PEER_BOUND < input_token_upper_bound(text) <= RUNG
    with_spread_peer(monkeypatch, tmp_path)
    members, _ = local_choices("summarization", text)
    assert PEER["backend_id"] not in members
    assert members == {LOCAL_HEAVY}
    # Control: a prompt inside the peer's bound still reaches it.
    control, _ = local_choices("summarization", hex_words(1000))
    assert PEER["backend_id"] in control


# ---- review of c9f3605f3e04, findings 2, 5, 8 and 9 --------------------------

# Read from the lab endpoint (POST /tokenize on the local rungs' vLLM, model
# Qwen3.8-27B, 2026-11-17T16:10Z). The texts come from seeded generators chosen
# without reference to the counting rule; the counts are the endpoint's, not the
# function's.


def _seeded_words(seed: int, alphabet: str, count: int, length: int = 12) -> str:
    rng = random.Random(seed)
    return "".join(
        "".join(rng.choice(alphabet) for _ in range(length)) + " " for _ in range(count)
    )


def _short_digit_words() -> str:
    rng = random.Random(25418)
    for _ in range(3000):  # the uuid draw the reading took first
        rng.getrandbits(128)
    return " ".join(
        f"v{rng.randrange(10)}{rng.choice('xyz')}{rng.randrange(100)}"
        for _ in range(12000)
    )


LAB_METERED_SAMPLES: dict[str, tuple[Any, int]] = {
    "digit_word_a1b2c3d": (lambda: "a1b2c3d " * 10752, 75265),
    "lower_run_12": (
        lambda: _seeded_words(25414, string.ascii_lowercase, 9216),
        60340,
    ),
    "upper_run_12": (
        lambda: _seeded_words(25415, string.ascii_uppercase, 9216),
        66478,
    ),
    "short_digit_words": (_short_digit_words, 58829),
}


@pytest.mark.parametrize("name", sorted(LAB_METERED_SAMPLES))
def test_input_token_bound_holds_lab_metered_samples_the_rule_did_not_choose(
    name: str,
) -> None:
    build, metered = LAB_METERED_SAMPLES[name]
    text = build()
    bound = input_token_upper_bound(text)
    # metered tokens are at or below the bound, so a prompt the gate admits at
    # bound 64512 meters at most 64512.
    assert metered <= bound, (name, metered, bound)
    if bound <= RUNG:
        assert metered <= RUNG


def test_clamp_refuses_a_prompt_with_no_output_room() -> None:
    # 17000 copies bound at 136000 tokens, above 131072 - 1024: no room at all.
    with pytest.raises(ValueError, match="no output room"):
        clamp_max_tokens("a1b2c3d " * 17000, "local", "local-coder", 65536)


def _bare_port() -> port_mod.LocalDelegationDispatchPort:
    class _Capture(port_mod.LocalDelegationDispatchPort):
        async def _dispatch_rungs(self, **kwargs: Any) -> dict[str, object]:
            return {}

    return object.__new__(_Capture)


def test_prompt_required_a_missing_prompt_raises_and_leaks_no_rung_skips() -> None:
    before = port_mod._RUNG_SKIPS.get()
    with pytest.raises(ValueError, match="needs a prompt"):
        asyncio.run(_bare_port()._dispatch_rungs_naming_skips(task_type="test"))
    assert port_mod._RUNG_SKIPS.get() is before


def test_prompt_required_a_non_string_prompt_is_refused() -> None:
    with pytest.raises(ValueError, match="must be a str"):
        asyncio.run(_bare_port()._dispatch_rungs_naming_skips(prompt=None))


def test_prompt_required_the_active_prompt_raises_outside_a_dispatch() -> None:
    with pytest.raises(LookupError):
        port_mod._ACTIVE_PROMPT.get()


def test_none_request_has_one_rule_at_every_routing_site() -> None:
    from types import SimpleNamespace

    from omnimarket.nodes.node_delegation_orchestrator.handlers import (
        handler_delegation_workflow as workflow_mod,
    )

    with pytest.raises(ValueError, match="no request"):
        workflow_mod._routing_prompt(
            cast(Any, SimpleNamespace(request=None, correlation_id="c-1"))
        )
    source = inspect.getsource(workflow_mod)
    assert "workflow.request.prompt if workflow.request" not in source
    assert source.count("_routing_prompt(workflow)") == 2
