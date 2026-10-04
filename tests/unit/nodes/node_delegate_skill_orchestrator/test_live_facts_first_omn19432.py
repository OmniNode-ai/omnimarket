# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Live readback: the facts-first prompt reaches a real model and is read (OMN-19432).

Opt-in only; CI never runs this. The hermetic suite
(``test_facts_first_reaches_the_model_omn19432.py``) proves the bytes handed to
the effect. That does not show a served model reads the numbered lines, so this
module drives the real chain, ``HandlerDelegateSkill`` ->
``LocalDelegationDispatchPort`` -> ``HandlerLlmDelegationCall`` -> a real HTTP
POST, on three code-review prompts whose defect sits on a known new-file line
the diff header places well away from 1. Each prompt runs once per arm:

* ``plain``: the contract's ``review`` shape is flipped to ``plain`` for the run;
* ``facts_first``: the shipped contract.

For each draw it prints one ``FACTS-FIRST-LIVE`` line with the status, the line
numbers the answer cites, and whether the known line is among them. What it
asserts is what does not depend on sampling: the facts arm POSTs the facts, the
plain arm does not, and every delegation reaches a terminal. The comparison is
the evidence, read from the printed lines; a served model's answers are not an
assertion.

Enable with::

    OMN_ALLOW_LIVE_LADDER=1 FACTS_FIRST_LIVE_ENDPOINT_URL=http://<host>:8000/v1/chat/completions \\
      FACTS_FIRST_LIVE_MODEL=Qwen3.8-27B uv run pytest -s \\
      tests/unit/nodes/node_delegate_skill_orchestrator/test_live_facts_first_omn19432.py
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
import yaml

from omnimarket.inference import task_class_authority as authority
from omnimarket.inference.task_class_authority import (
    _DEFAULT_AUTHORITY_PATH,
    ModelTaskClassAuthority,
)
from omnimarket.nodes.node_delegate_skill_orchestrator.handlers.handler_delegate_skill import (
    HandlerDelegateSkill,
)
from omnimarket.nodes.node_delegate_skill_orchestrator.models.model_delegate_skill_request import (
    ModelDelegateSkillRequest,
)
from omnimarket.nodes.node_delegate_skill_orchestrator.ports.port_local_delegation_dispatch import (
    LocalDelegationDispatchPort,
)
from omnimarket.nodes.node_facts_first_prompt_compute.handlers.handler_facts_first_prompt import (
    FACTS_FIRST_HEADER,
)
from omnimarket.routing import delegation_backend_resolution

pytestmark = pytest.mark.skipif(
    os.environ.get("OMN_ALLOW_LIVE_LADDER") != "1",
    reason="live LLM call; set OMN_ALLOW_LIVE_LADDER=1 and FACTS_FIRST_LIVE_ENDPOINT_URL",
)

_DRAWS = 3
_INSTRUCTION = (
    "Review this change. List each defect with the line number in the new file "
    "where it occurs. You must cite a line number for each defect. Do not invent "
    "defects. Answer in at most 80 words."
)

# (case id, old/new start line, rows, 1-based index into the rows of the defect)
_CASES: tuple[tuple[str, int, tuple[str, ...], int], ...] = (
    (
        "undefined-name",
        41,
        (
            " def total(items):",
            "     result = 0",
            "     for item in items:",
            "-        result += item.price",
            "+        result += item.price * item.qty",
            "+    result -= discount",
            "     return result",
        ),
        6,
    ),
    (
        "off-by-one",
        118,
        (
            " def last_n(values, n):",
            "     if n <= 0:",
            "         return []",
            "-    return values[-n:]",
            "+    return values[-n - 1 :]",
        ),
        5,
    ),
    (
        "inverted-condition",
        207,
        (
            " def allowed(user, resource):",
            "     if user.is_admin:",
            "         return True",
            "-    return resource.owner == user.id",
            "+    return resource.owner != user.id",
        ),
        5,
    ),
)


def _diff(start: int, rows: tuple[str, ...]) -> str:
    old = sum(1 for r in rows if r[0] in " -")
    new = sum(1 for r in rows if r[0] in " +")
    return f"```diff\n@@ -{start},{old} +{start},{new} @@\n" + "\n".join(rows) + "\n```"


def _defect_line(start: int, rows: tuple[str, ...], row_index: int) -> int:
    """The new-file line of the 1-based ``row_index``-th row."""
    return start + sum(1 for r in rows[: row_index - 1] if r[0] in " +")


def _cited(answer: str) -> set[int]:
    return {int(n) for n in re.findall(r"(?i)\blines?\s*#?(\d{1,5})\b", answer)}


def _backends() -> list[dict[str, Any]]:
    url = os.environ.get("FACTS_FIRST_LIVE_ENDPOINT_URL", "").strip()
    if not url:
        pytest.skip("FACTS_FIRST_LIVE_ENDPOINT_URL is unset; no model to ask")
    return [
        {
            "backend_id": "live-facts-first",
            "endpoint_url": url,
            "model_name": os.environ.get("FACTS_FIRST_LIVE_MODEL", "Qwen3.8-27B"),
            "tier": "local",
            "max_tokens": 8192,
            "timeout_ms": 200000,
            "capabilities": ["review"],
        }
    ]


async def _delegate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, prompt: str
) -> tuple[str, str, dict[str, Any]]:
    from omnimarket.nodes.node_llm_delegation_call_effect.handlers import (
        handler_llm_delegation_call as effect_module,
    )

    monkeypatch.setattr(
        delegation_backend_resolution, "load_bifrost_backends", lambda **_: _backends()
    )
    posted: dict[str, Any] = {}
    real_post = effect_module.transport.post_chat_completion

    def _observing_post(*, payload: dict[str, Any], **kwargs: Any) -> Any:
        posted.clear()
        posted.update(json.loads(json.dumps(payload)))
        return real_post(payload=payload, **kwargs)

    monkeypatch.setattr(
        effect_module.transport, "post_chat_completion", _observing_post
    )
    port = LocalDelegationDispatchPort(
        evidence_db_path=tmp_path / f"live-{uuid4()}.sqlite",
        effect_process_boundary=False,
    )
    response = await HandlerDelegateSkill(dispatch_port=port).handle(
        ModelDelegateSkillRequest(
            prompt=prompt,
            task_type="review",
            source="external-client",
            backend_id="live-facts-first",
            max_tokens=4096,
        )
    )
    return response.status, str(response.response or "").strip(), posted


def _with_review_shape(monkeypatch: pytest.MonkeyPatch, shape: str) -> None:
    raw = yaml.safe_load(_DEFAULT_AUTHORITY_PATH.read_text(encoding="utf-8"))
    raw["task_classes"]["review"]["prompt_shape"] = shape
    flipped = ModelTaskClassAuthority.model_validate(raw)
    monkeypatch.setattr(authority, "_delegation_task_class_authority", lambda: flipped)


@pytest.mark.live_model
@pytest.mark.parametrize("arm", ["plain", "facts_first"])
async def test_a_served_model_reviews_a_diff_under_each_prompt_shape(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, arm: str
) -> None:
    _with_review_shape(monkeypatch, arm)
    delivered = 0
    right = 0
    draws = 0
    for case_id, start, rows, row_index in _CASES:
        known = _defect_line(start, rows, row_index)
        prompt = f"{_INSTRUCTION}\n\n{_diff(start, rows)}"
        for draw in range(_DRAWS):
            status, answer, posted = await _delegate(tmp_path, monkeypatch, prompt)
            user_turn = posted["messages"][-1]["content"]
            assert (FACTS_FIRST_HEADER in user_turn) is (arm == "facts_first")
            cited = _cited(answer)
            draws += 1
            delivered += status == "completed"
            right += known in cited
            print(
                f"FACTS-FIRST-LIVE arm={arm} case={case_id} draw={draw} "
                f"status={status} known_line={known} cited={sorted(cited)} "
                f"hit={known in cited}"
            )
    print(
        f"FACTS-FIRST-LIVE-TOTAL arm={arm} draws={draws} completed={delivered} "
        f"known_line_cited={right}"
    )
    assert draws == len(_CASES) * _DRAWS
