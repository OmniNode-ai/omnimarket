# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A failed model test says why in plain words (OMN-23413).

The reasons below are the CLI's own, read from `onex models add local --json`
on 2026-11-13 (hosts replaced). The TUI showed their first 160 characters,
which were routing boilerplate, and the cause came after the cut. Failure modes:
  R1  the cause is hidden behind "returned_to_caller ... do the task in this session".
  R2  an internal tag or ticket note is shown instead of the sentence.
  R3  an unreachable server shows the raw stderr line, uncut.
  R4  a long reason is cut mid-word, or a short one is changed.
"""

from __future__ import annotations

import pytest

from omnimarket.nodes.node_onex_setup_effect.cli_tui import plain_reason

pytestmark = pytest.mark.unit

_CUT_OFF = (
    "returned_to_caller: quality_veto; no model you chose finished the task; do the "
    "task in this session (reason: quality_veto; last error: pinned_backend_failed: "
    "the run is pinned to backend 'local-url' and does not move to another backend: "
    "DELIVERABLE_EXTRACTION: ambiguous_unmarked_deliverable; WEAK_OUTPUT: provider "
    "reported finish_reason=length -- the response was cut off by the output-token "
    "budget before the model finished gener..."
)
_NOT_SERVED = (
    "returned_to_caller: every_rung_failed; no model you chose finished the task; do "
    "the task in this session (reason: every_rung_failed; last error: "
    "pinned_backend_failed: the run is pinned to backend 'local-url-2' and does not "
    "move to another backend: model_attribution_mismatch: configured "
    "model_name='no-such-model' is not in the served ids ['Qwen3.8-27B'] reported by "
    "http://lab.example:8000/v1/models (OMN-16419 fail-closed guard — ..."
)
_UNREACHABLE = (
    "Error: http://127.0.0.1:9 does not answer an OpenAI-compatible models or chat "
    "request (GET /v1/models: ConnectError: [Errno 61] Connection refused; chat, by "
    "one delegation pinned to it: returned_to_caller: no_rung_reachable; no model you "
    "chose finished the task; do the task in this session (reason:"
)


@pytest.mark.parametrize(
    ("raw", "plain"),
    [
        (
            _CUT_OFF,
            "the response was cut off by the output-token budget before the model "
            "finished…",
        ),
        (
            _NOT_SERVED,
            "configured model_name='no-such-model' is not in the served ids "
            "['Qwen3.8-27B'] reported by http://lab.example:8000/v1/models",
        ),
        (
            _UNREACHABLE,
            "http://127.0.0.1:9 does not answer an OpenAI-compatible models or chat "
            "request (GET /v1/models: ConnectError: [Errno 61] Connection refused)",
        ),
    ],
    ids=["cut-off", "not-served", "unreachable"],
)
def test_the_cause_is_shown_not_the_routing(raw: str, plain: str) -> None:
    """R1, R2, R3."""
    assert plain_reason(raw, limit=400) == plain


def test_a_short_reason_is_unchanged() -> None:
    """R4."""
    assert plain_reason("connection refused", limit=160) == "connection refused"


def test_a_long_reason_is_cut_at_a_word() -> None:
    """R4."""
    shown = plain_reason("word " * 60, limit=40)
    assert len(shown) <= 40
    assert shown.endswith("word…")


def test_nothing_to_say_is_said_plainly() -> None:
    assert plain_reason("   ", limit=160) == ""
