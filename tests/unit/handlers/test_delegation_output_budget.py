# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The output budget reader: overlay values, the measured input, absences and refusals (OMN-26640)."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest

from omnimarket.handlers.delegation_output_budget import (
    OutputBudgetRefusedError,
    read_output_budget_setting,
    resolve_output_budget,
)
from omnimarket.handlers.node_overlay_reader import NodeOverlayError
from omnimarket.handlers.routing.input_token_bound import input_token_upper_bound
from omnimarket.handlers.routing.measured_correct_ceiling import CODE_BUDGET_OVERLAY_ID
from omnimarket.handlers.routing.vllm_input_token_meter import (
    InputTokenMeterError,
    VllmTokenizeMeter,
)

pytestmark = pytest.mark.unit

ENDPOINT = "http://192.0.2.7:8111/v1/chat/completions"
MESSAGES = [{"role": "user", "content": "Write a function that adds two numbers."}]


def _env(root: Path) -> dict[str, str]:
    return {"ONEX_SKILL_OVERLAY_ROOTS": str(root)}


def _write_overlay(
    root: Path, window: int = 65536, margin: int = 512, ceiling: int = 65536
) -> None:
    path = root / CODE_BUDGET_OVERLAY_ID / "overlay.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "class_budgets_s:\n  code_generation: 600\n"
        "sizing_margin: 1.5\nround_up_to_s: 60\nmin_completed_samples: 3\nceiling_s: 600\n"
        "input_ceilings:\n  local-coder:\n    code_generation: 65536\n"
        "output_budgets:\n  inkling-lab:\n"
        f"    window: {window}\n    margin: {margin}\n    floor: 1024\n"
        f"    ceiling: {ceiling}\n    measure_timeout_s: 5\n",
        encoding="utf-8",
    )


class _Meter:
    def __init__(self, count: int | Exception) -> None:
        self._count = count
        self.calls: list[dict[str, Any]] = []

    def count(
        self,
        *,
        endpoint_ref: str,
        model_id: str,
        messages: Sequence[Mapping[str, str]],
        timeout_s: float,
    ) -> int:
        self.calls.append(
            {
                "endpoint_ref": endpoint_ref,
                "model_id": model_id,
                "messages": list(messages),
                "timeout_s": timeout_s,
            }
        )
        if isinstance(self._count, Exception):
            raise self._count
        return self._count


def _resolve(root: Path, meter: _Meter, requested: int | None = None, **kw: Any):
    return resolve_output_budget(
        backend_id=kw.pop("backend_id", "inkling-lab"),
        endpoint_ref=ENDPOINT,
        model_id="Inkling-2-Mini",
        messages=MESSAGES,
        requested=requested,
        meter=meter,
        environ=_env(root),
    )


def test_budget_is_window_less_the_measured_input_less_the_margin(
    tmp_path: Path,
) -> None:
    _write_overlay(tmp_path)
    meter = _Meter(1471)
    resolution = _resolve(tmp_path, meter)
    assert resolution is not None
    assert resolution.tokens == 65536 - 1471 - 512
    assert resolution.input_tokens == 1471
    assert resolution.input_source == "measured"
    assert meter.calls == [
        {
            "endpoint_ref": ENDPOINT,
            "model_id": "Inkling-2-Mini",
            "messages": MESSAGES,
            "timeout_s": 5,
        }
    ]


def test_a_caller_may_ask_for_less_never_more(tmp_path: Path) -> None:
    _write_overlay(tmp_path)
    low = _resolve(tmp_path, _Meter(1000), requested=4096)
    high = _resolve(tmp_path, _Meter(1000), requested=10**6)
    assert low is not None
    assert high is not None
    assert low.tokens == 4096
    assert high.tokens == 65536 - 1000 - 512


def test_the_overlay_numbers_move_the_budget(tmp_path: Path) -> None:
    _write_overlay(tmp_path, window=131072, margin=2048, ceiling=131072)
    resolution = _resolve(tmp_path, _Meter(3000))
    assert resolution is not None
    assert resolution.tokens == 131072 - 3000 - 2048


def test_a_backend_the_overlay_does_not_name_has_no_budget(tmp_path: Path) -> None:
    _write_overlay(tmp_path)
    meter = _Meter(10)
    assert _resolve(tmp_path, meter, backend_id="local-coder") is None
    assert meter.calls == []
    assert read_output_budget_setting("local-coder", _env(tmp_path)) is None


def test_an_install_with_no_overlay_has_no_budget(tmp_path: Path) -> None:
    assert read_output_budget_setting("inkling-lab", _env(tmp_path)) is None
    assert _resolve(tmp_path, _Meter(10)) is None


def test_an_unreadable_overlay_raises_never_a_default(tmp_path: Path) -> None:
    path = tmp_path / CODE_BUDGET_OVERLAY_ID / "overlay.yaml"
    path.parent.mkdir(parents=True)
    path.write_text("output_budgets: [broken", encoding="utf-8")
    with pytest.raises(NodeOverlayError):
        read_output_budget_setting("inkling-lab", _env(tmp_path))


def test_a_prompt_with_no_room_is_refused_not_sent_with_one_token(
    tmp_path: Path,
) -> None:
    _write_overlay(tmp_path)
    with pytest.raises(OutputBudgetRefusedError, match="room"):
        _resolve(tmp_path, _Meter(65536 - 512 - 1023))


def test_a_meter_that_fails_falls_back_to_the_upper_bound_and_says_so(
    tmp_path: Path,
) -> None:
    _write_overlay(tmp_path)
    resolution = _resolve(tmp_path, _Meter(InputTokenMeterError("refused")))
    bound = input_token_upper_bound(MESSAGES[0]["content"])
    assert resolution is not None
    assert resolution.input_source == "upper_bound"
    assert resolution.input_tokens >= bound
    assert resolution.tokens == 65536 - resolution.input_tokens - 512


def test_the_vllm_meter_reads_count_from_the_servers_tokenize(tmp_path: Path) -> None:
    posts: list[tuple[str, dict[str, Any], float]] = []

    def post(url: str, body: dict[str, Any], timeout_s: float) -> dict[str, Any]:
        posts.append((url, body, timeout_s))
        return {"count": 23, "max_model_len": 65536, "tokens": [1, 2, 3]}

    count = VllmTokenizeMeter(post=post).count(
        endpoint_ref=ENDPOINT,
        model_id="Inkling-2-Mini",
        messages=MESSAGES,
        timeout_s=5,
    )
    assert count == 23
    assert posts == [
        (
            "http://192.0.2.7:8111/tokenize",
            {"model": "Inkling-2-Mini", "messages": MESSAGES},
            5,
        )
    ]


@pytest.mark.parametrize(
    "reply", [{}, {"count": "x"}, {"count": -1}, {"count": True}, []]
)
def test_the_vllm_meter_refuses_a_reply_without_a_count(reply: Any) -> None:
    meter = VllmTokenizeMeter(post=lambda _url, _body, _timeout_s: reply)
    with pytest.raises(InputTokenMeterError):
        meter.count(
            endpoint_ref=ENDPOINT,
            model_id="m",
            messages=MESSAGES,
            timeout_s=1,
        )


def test_the_vllm_meter_refuses_an_endpoint_that_is_not_a_chat_completions_url() -> (
    None
):
    meter = VllmTokenizeMeter(post=lambda _url, _body, _timeout_s: {"count": 1})
    with pytest.raises(InputTokenMeterError):
        meter.count(
            endpoint_ref="http://192.0.2.7:8111/other",
            model_id="m",
            messages=MESSAGES,
            timeout_s=1,
        )
