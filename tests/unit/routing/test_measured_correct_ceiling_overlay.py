# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The routing ceiling is the overlay value, read on every call (OMN-23631).

``measured_correct_ceiling`` reads ``node_delegation_code_budget_compute``'s
overlay through its ``input_ceiling`` operation; no packaged number backs it.
These tests prove the overlay decides: a lowered overlay moves a 4560-token
code_generation task off local-coder (and the old packaged 8169, injected as a
break, un-moves it), an absent or broken overlay raises instead of falling
back, the gate names the overlay key in its refusal, and the grounded budget
follows the ceiling. Every fixture is in memory: the tier ladder comes from the
packaged routing contract and the backends are fakes, so no endpoint is called.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Literal
from uuid import uuid4

import pytest
import yaml

from omnimarket.handlers.node_overlay_reader import (
    NodeOverlayError,
    NodeOverlayNotFoundError,
)
from omnimarket.handlers.routing import measured_correct_ceiling as ceiling_module
from omnimarket.handlers.routing.measured_correct_ceiling import (
    exceeds_measured_correct_ceiling,
    grounded_input_budget,
    load_grounding_budget_by_task_class,
    refuse_above_measured_correct_ceiling,
)
from omnimarket.models.delegation.wire import ModelDelegationConfig, ModelTierModel
from omnimarket.models.delegation.wire.model_attempt_rubric_verdict import (
    ModelAttemptRubricVerdict,
)
from omnimarket.models.delegation.wire.model_quality_gate import ModelQualityGateResult
from omnimarket.nodes.node_delegation_routing_reducer.handlers import (
    handler_delegation_routing as routing,
)
from tests.placed_routing_config import placed_routing_config

pytestmark = pytest.mark.unit

_Outcome = Literal["PASS", "FAIL", "UNDETERMINED"]
_CODER = "local-coder"
_TASK = "code_generation"
_CEILING = 8169  # the fixture overlay's value for local-coder code_generation.
_LOWERED = 2000  # the failing control's overlay value for the same pair.
_OVERLAY_KEY = "delegation.code_budget.input_ceilings.local-coder.code_generation"

_FIXTURE_OVERLAY = (
    Path(__file__).resolve().parents[2]
    / "nodes"
    / "node_delegation_code_budget_compute"
    / "fixtures"
    / "overlays"
    / "node_delegation_code_budget_compute"
    / "overlay.yaml"
)


def _prompt_of(tokens: int) -> str:
    """A prompt that measures ``tokens`` in the routing estimator's units."""
    return "x" * (tokens * 4)


def _fake_backends(
    config: ModelDelegationConfig,
) -> dict[str, routing.BifrostBackendRef]:
    refs = {model.backend_ref for tier in config.tiers for model in tier.models}
    return {
        ref: routing.BifrostBackendRef(
            endpoint_url=f"http://{ref}.invalid/v1",
            model_name=f"model-{ref}",
            timeout_ms=1000,
            max_tokens=256,
        )
        for ref in refs
    }


def _first_route(tokens: int) -> tuple[str, str] | None:
    """The tier and backend the ladder offers a code_generation task first."""
    config = placed_routing_config()
    backends = _fake_backends(config)
    for tier in config.tiers:
        selected: ModelTierModel | None = routing._select_model_for_task(
            tier.models,
            _TASK,
            tokens,
            backends,
            require_credential=False,
        )
        if selected is not None:
            return tier.name, selected.backend_ref
    return None


def _gate_passed(score: float) -> ModelQualityGateResult:
    return ModelQualityGateResult(
        correlation_id=uuid4(), passed=True, quality_score=score
    )


def _verdict(
    outcome: _Outcome, **criteria: tuple[str, ...]
) -> ModelAttemptRubricVerdict:
    return ModelAttemptRubricVerdict(
        rubric_version="v-test",
        task_class=_TASK,
        outcome=outcome,
        **criteria,
    )


def _write_overlay_with_ceiling(tmp_path: Path, ceiling: int) -> Path:
    """A root holding the fixture overlay with local-coder's ceiling replaced."""
    data = yaml.safe_load(_FIXTURE_OVERLAY.read_text(encoding="utf-8"))
    data["input_ceilings"][_CODER][_TASK] = ceiling
    root = tmp_path / "overlay_roots"
    target = root / "node_delegation_code_budget_compute" / "overlay.yaml"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(yaml.safe_dump(data), encoding="utf-8")
    return root


def _lowered_overlay_keeps_local_coder_off_the_4560_token_task(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The lowered-overlay assertion of the failing control, as a helper.

    Under a ``ceiling 2000`` overlay the 4560-token code_generation task must
    not be offered to local-coder; raises :class:`AssertionError` when it is.
    """
    monkeypatch.setenv(
        "ONEX_SKILL_OVERLAY_ROOTS", str(_write_overlay_with_ceiling(tmp_path, _LOWERED))
    )
    route = _first_route(4560)
    assert route is not None
    assert route[1] != _CODER, route


def test_fixture_overlay_routes_24579_off_and_8169_to_local_coder() -> None:
    """The autouse fixture overlay's 8169 decides the route both ways."""
    route = _first_route(24579)
    assert route is not None
    assert route[1] != _CODER, route
    assert _first_route(_CEILING) == ("local", _CODER)


def test_lowered_overlay_moves_the_4560_token_task_off_local_coder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The overlay value, not a packaged number, decides the route.

    The same 4560-token task routes to local-coder under the fixture overlay's
    8169 and off it under a lowered overlay's 2000.
    """
    assert _first_route(4560) == ("local", _CODER)
    _lowered_overlay_keeps_local_coder_off_the_4560_token_task(tmp_path, monkeypatch)


def test_refusal_names_the_lowered_ceiling_and_its_overlay_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The gate's refusal carries the overlay ceiling and the key it came from."""
    monkeypatch.setenv(
        "ONEX_SKILL_OVERLAY_ROOTS", str(_write_overlay_with_ceiling(tmp_path, _LOWERED))
    )
    decided = refuse_above_measured_correct_ceiling(
        _gate_passed(0.733),
        _verdict("UNDETERMINED", undetermined_criteria=("passes_existing_tests",)),
        backend_ref=_CODER,
        task_class=_TASK,
        request_text=_prompt_of(4560),
    )
    assert decided.passed is False
    ceiling_reasons = [
        reason
        for reason in decided.failure_reasons
        if "above_measured_correct_ceiling" in reason
    ]
    assert len(ceiling_reasons) == 1
    assert "ceiling 2000" in ceiling_reasons[0]
    assert _OVERLAY_KEY in ceiling_reasons[0]


def test_absent_overlay_declares_no_ceiling_and_broken_overlay_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A clean install names no overlay: no ceiling applies. A broken overlay raises.

    A roots list that names no root holding the overlay declares no ceilings (the
    customer clean-install gate runs there); a present overlay that is not a
    mapping is broken, and that is a raise, never a default.
    """
    monkeypatch.setenv("ONEX_SKILL_OVERLAY_ROOTS", str(tmp_path / "absent_roots"))
    assert exceeds_measured_correct_ceiling(_CODER, _TASK, 4560) is False
    assert ceiling_module.measured_correct_ceiling(_CODER, _TASK) is None

    root = tmp_path / "broken_roots"
    target = root / "node_delegation_code_budget_compute" / "overlay.yaml"
    target.parent.mkdir(parents=True)
    target.write_text("- just\n- a\n- list\n", encoding="utf-8")
    monkeypatch.setenv("ONEX_SKILL_OVERLAY_ROOTS", str(root))
    with pytest.raises(NodeOverlayError) as error:
        exceeds_measured_correct_ceiling(_CODER, _TASK, 4560)
    assert not isinstance(error.value, NodeOverlayNotFoundError)


def test_old_packaged_value_as_a_break_un_does_the_lowered_control(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The failing control fails under the old packaged ceiling (OMN-23631).

    The retired behaviour hard-coded local-coder's code_generation ceiling at
    8169 and gave every other pair none; under it the lowered overlay has no
    effect and the 4560-token task goes to local-coder again, so the helper's
    assertion fails.
    """

    def _old_packaged_ceiling(
        backend_ref: str,
        task_type: str,
        environ: Mapping[str, str] | None = None,
    ) -> int | None:
        del environ  # the old behaviour never read the roots
        if (backend_ref, task_type) == (_CODER, _TASK):
            return _CEILING
        return None

    monkeypatch.setattr(
        ceiling_module, "measured_correct_ceiling", _old_packaged_ceiling
    )
    with pytest.raises(AssertionError):
        _lowered_overlay_keeps_local_coder_off_the_4560_token_task(
            tmp_path, monkeypatch
        )


def test_grounded_input_budget_follows_the_overlay(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    code_budget_overlay_root: Path,
) -> None:
    """The budget is the overlay ceiling when one is named, the packaged one otherwise."""
    assert grounded_input_budget(_CODER, _TASK) == _CEILING
    monkeypatch.setenv(
        "ONEX_SKILL_OVERLAY_ROOTS", str(_write_overlay_with_ceiling(tmp_path, _LOWERED))
    )
    assert grounded_input_budget(_CODER, _TASK) == _LOWERED

    # local-heavy-reasoning names no ceiling in the overlay, so its budget is
    # the packaged one in configs/grounding_budget_by_task_class.yaml.
    monkeypatch.setenv("ONEX_SKILL_OVERLAY_ROOTS", str(code_budget_overlay_root))
    assert grounded_input_budget("local-heavy-reasoning", _TASK) == 32000
    assert (
        load_grounding_budget_by_task_class().budget_for("local-heavy-reasoning", _TASK)
        == 32000
    )
