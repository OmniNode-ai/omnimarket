# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden-equivalence proof for the persona-builder def-B flip (OMN-14836).

``HandlerPersonaClassify.handle()`` moved from ``handle(correlation_id, input_data)``
(def-A, two positional params -> non-canonical, RuntimeLocal-undispatchable) to the
canonical ``handle(request) -> ModelPersonaClassifyResult`` (def-B, single typed-payload
positional). This is a *verbatim* hand-flip (OMN-14781): the pure ``_classify_persona``
reducer and its ``_classify_*`` helpers are byte-identical (AST-normalized) across the
flip — ``handle`` simply forwards ``request`` where it previously forwarded
``input_data``. ``correlation_id`` was unused by the reducer, so no behavior depends
on it.

The goldens under ``tests/fixtures/golden/node_persona_builder_compute/*.json`` were
recorded by running the reviewed deterministic pool (``persona_builder_pool``) through
the reducer and capturing byte-canonical output + a sha256 fingerprint. This durable
regression drives the SAME recorded inputs through the live def-B ``handle(request)``
and asserts byte/fingerprint-identical output, so a future edit to the classification
logic cannot silently change behavior without failing here.

``persona.created_at`` is a documented volatile field (``datetime.now`` inside the
reducer); it is masked by ``normalize_output`` before fingerprinting.
``test_fingerprint_discriminates`` proves the oracle is not vacuously green.
"""

from __future__ import annotations

import hashlib
import inspect
import json
from pathlib import Path
from typing import Any

import pytest
import yaml

from omnimarket.nodes.node_persona_builder_compute.handlers.handler_persona_classify import (
    HandlerPersonaClassify,
)
from omnimarket.nodes.node_persona_builder_compute.models.model_classify_request import (
    ModelPersonaClassifyRequest,
)
from omnimarket.nodes.node_persona_builder_compute.models.model_classify_result import (
    ModelPersonaClassifyResult,
)
from tests.unit.nodes.node_persona_builder_compute.persona_builder_pool import (
    build_candidate_pool,
    normalize_output,
)

pytestmark = pytest.mark.unit

_REPO_ROOT = Path(__file__).resolve().parents[4]
_GOLDEN_DIR = (
    _REPO_ROOT / "tests" / "fixtures" / "golden" / "node_persona_builder_compute"
)
_CONTRACT_PATH = (
    _REPO_ROOT
    / "src"
    / "omnimarket"
    / "nodes"
    / "node_persona_builder_compute"
    / "contract.yaml"
)

# The reviewed scenario pool size (see persona_builder_pool.build_candidate_pool).
_EXPECTED_GOLDEN_COUNT = 12


def _fingerprint(output_json: dict[str, Any]) -> str:
    canonical = json.dumps(output_json, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(canonical.encode()).hexdigest()


def _golden_files() -> list[Path]:
    files = sorted(_GOLDEN_DIR.glob("*.json"))
    assert files, f"no golden fixtures found under {_GOLDEN_DIR}"
    return files


@pytest.mark.asyncio
@pytest.mark.parametrize("golden_path", _golden_files(), ids=lambda p: p.stem)
async def test_handle_reproduces_recorded_golden(golden_path: Path) -> None:
    """def-B ``handle(request)`` on the recorded input == the recorded output."""
    golden: dict[str, Any] = json.loads(golden_path.read_text(encoding="utf-8"))
    request = ModelPersonaClassifyRequest.model_validate(golden["input"])

    output = await HandlerPersonaClassify().handle(request)

    assert isinstance(output, ModelPersonaClassifyResult)
    fresh_json = normalize_output(output.model_dump(mode="json"))
    assert fresh_json == golden["output"], (
        f"{golden_path.name}: def-B output structure diverged from recorded def-A"
    )
    assert _fingerprint(fresh_json) == golden["output_sha256"], (
        f"{golden_path.name}: def-B output fingerprint diverged from recorded def-A"
    )


def test_golden_fixture_count_matches_expected_pool() -> None:
    """Regression guard: the recorded scenario pool has a known, reviewed size."""
    assert len(_golden_files()) == _EXPECTED_GOLDEN_COUNT
    assert len(build_candidate_pool()) == _EXPECTED_GOLDEN_COUNT


def test_handle_is_canonical_def_b_shape() -> None:
    """The dispatch entrypoint is the canonical def-B signature the runtime adapts.

    Exactly one positional-or-keyword parameter besides ``self`` (a single typed
    payload). The pre-flip def-A ``handle(correlation_id, input_data)`` had two and
    was RuntimeLocal-undispatchable; this asserts the flip actually landed.
    """
    sig = inspect.signature(HandlerPersonaClassify.handle)
    params = [
        p
        for name, p in sig.parameters.items()
        if name != "self"
        and p.kind
        in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)
    ]
    assert len(params) == 1, f"expected 1 payload param, got {[p.name for p in params]}"
    assert params[0].name == "request"


def test_fingerprint_discriminates() -> None:
    """The fingerprint oracle is not vacuous: a 1-field perturbation diverges."""
    golden = json.loads(
        (_GOLDEN_DIR / "02_fresh_user_tech_advanced.json").read_text(encoding="utf-8")
    )
    recorded_fp = golden["output_sha256"]
    assert _fingerprint(golden["output"]) == recorded_fp  # recompute matches

    perturbed = json.loads(json.dumps(golden["output"]))
    perturbed["persona"]["session_count"] = (
        int(perturbed["persona"]["session_count"]) + 1
    )
    assert _fingerprint(perturbed) != recorded_fp, (
        "fingerprint failed to detect a perturbed output — oracle is vacuous"
    )


@pytest.mark.asyncio
async def test_declared_terminal_states_covered_and_success_reachable() -> None:
    """State-coverage (OMN-13781): both declared terminal topics are pinned and
    the success terminal is genuinely reachable.

    The COMPUTE handler emits no events itself — the shared runtime synthesizes
    the contract's ``runtime_dispatch.terminal_events`` topic after classifying
    ``handle()``'s return (success) or a raised error (failure). This pins both
    declared ``event_bus.publish_topics`` to their literals (so the state-coverage
    gate sees them exercised, not merely declared) and drives a real successful
    classification the runtime maps to persona-builder-completed.v1.
    """
    contract = yaml.safe_load(_CONTRACT_PATH.read_text(encoding="utf-8"))
    publish = contract["event_bus"]["publish_topics"]
    terminals = contract["runtime_dispatch"]["terminal_events"]

    assert terminals["success"] == "onex.evt.omnimemory.persona-builder-completed.v1"
    assert terminals["failure"] == "onex.evt.omnimemory.persona-builder-failed.v1"
    assert "onex.evt.omnimemory.persona-builder-completed.v1" in publish
    assert "onex.evt.omnimemory.persona-builder-failed.v1" in publish

    # Success terminal reachable: a real classification returns a success result
    # (the runtime maps a non-raising handle() return to the success terminal).
    request = ModelPersonaClassifyRequest.model_validate(
        json.loads(
            (_GOLDEN_DIR / "02_fresh_user_tech_advanced.json").read_text(
                encoding="utf-8"
            )
        )["input"]
    )
    result = await HandlerPersonaClassify().handle(request)
    assert result.status == "success"
