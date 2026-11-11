# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden and error chains of the input_ceiling operation (OMN-23631), each with named mutations that turn the chain red.

The golden chain reads the contract and resolves every named pair through the
handler; the error chain refuses an unnamed pair and, through
measured_correct_ceiling, reads no ceiling from an absent overlay and raises on a broken one and reads a copy
of the fixture overlay. Every mutation replaces one rule and runs the chain it
breaks; the mutated chain must raise AssertionError, which shows the assertion
can fail. The unmutated chains are green in the controls.
"""

from __future__ import annotations

import importlib.resources
import sys
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

import pytest
import yaml

from omnimarket.handlers.node_overlay_reader import (
    OVERLAY_ROOTS_ENV,
    NodeOverlayError,
)
from omnimarket.handlers.routing.measured_correct_ceiling import (
    measured_correct_ceiling,
)
from omnimarket.models.model_delegation_code_budget import (
    EnumCodeBudgetRefusal,
    ModelCodeBudgetOverlay,
    ModelCodeBudgetRequest,
    ModelCodeBudgetResult,
    input_ceiling_overlay_key,
)

pytestmark = pytest.mark.unit

NAME = "node_delegation_code_budget_compute"
OVERLAY_FILE = (
    Path(__file__).resolve().parent / "fixtures" / "overlays" / NAME / "overlay.yaml"
)

# An overlay of the pre-OMN-23631 shape: valid for its time, and now incomplete.
LEGACY_OVERLAY_YAML = """\
class_budgets_s:
  code_generation: 480
sizing_margin: 1.5
round_up_to_s: 60
min_completed_samples: 3
ceiling_s: 600
"""

# A document that parses but is not a mapping of overlay fields.
NOT_A_MAPPING_OVERLAY_YAML = "- a\n- b\n"


def contract() -> dict[str, Any]:
    """Load the contract YAML for the node."""
    files = importlib.resources.files(f"omnimarket.nodes.{NAME}")
    path = files.joinpath("contract.yaml")
    with path.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def resolve_handler() -> Any:
    """The handler class the packaged contract routes its one operation to."""
    [entry] = contract()["handler_routing"]["handlers"]
    assert entry["operation"] == "delegation_code_budget"
    module = importlib.import_module(entry["handler"]["module"])
    return getattr(module, entry["handler"]["name"])


def load_overlay() -> ModelCodeBudgetOverlay:
    """Load and validate the fixture overlay."""
    with OVERLAY_FILE.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    return ModelCodeBudgetOverlay.model_validate(data)


def build_request(
    operation: str, task_class: str, backend: str | None = None
) -> ModelCodeBudgetRequest:
    """Build a request with the fixture overlay."""
    return ModelCodeBudgetRequest(
        operation=operation,
        task_class=task_class,
        backend=backend,
        overlay=load_overlay(),
    )


def overlay_root(root: Path, text: str) -> dict[str, str]:
    """Write ``text`` as the node's overlay under ``root`` and return its environ."""
    (root / NAME).mkdir(parents=True)
    (root / NAME / "overlay.yaml").write_text(text, encoding="utf-8")
    return {OVERLAY_ROOTS_ENV: str(root)}


def assert_ceiling_read_raises(
    exc_type: type[Exception], read: Callable[[], int | None]
) -> None:
    """Assert the overlay read raises ``exc_type`` and never returns a ceiling.

    A read that returns anything — a number, or the None of an unnamed pair —
    is a default in disguise, and fails here with the returned value named.
    """
    try:
        returned = read()
    except exc_type:
        return
    except Exception as exc:
        raise AssertionError(
            f"expected {exc_type.__name__}, raised {type(exc).__name__}"
        ) from exc
    raise AssertionError(f"expected {exc_type.__name__}, returned {returned!r}")


def run_golden_chain_input_ceiling() -> None:
    """Run the golden chain: a pure compute contract whose named pairs resolve."""
    c = contract()
    assert c["node_type"] == "compute"
    assert c["descriptor"]["side_effects"] == []
    handler = resolve_handler()()

    named = (
        ("local-coder", "code_generation", 8169),
        ("fixture-backend", "code_generation", 1000),
        ("fixture-backend", "test", 2000),
    )
    for backend, task_class, expected in named:
        result = handler.handle(build_request("input_ceiling", task_class, backend))
        assert isinstance(result, ModelCodeBudgetResult)
        assert result.input_ceiling_tokens == expected
        assert result.refusal is None
        assert result.source == "overlay"
        assert result.overlay_key == input_ceiling_overlay_key(backend, task_class)


def run_error_chain_input_ceiling(tmp_path: Path) -> None:
    """Run the error chain: an unnamed pair is refused; a broken overlay raises."""
    handler = resolve_handler()()

    # The overlay does not name (local-coder, test): refused, key named, no tokens.
    result = handler.handle(build_request("input_ceiling", "test", "local-coder"))
    assert result.input_ceiling_tokens is None
    assert result.refusal is EnumCodeBudgetRefusal.UNNAMED_INPUT_CEILING
    assert input_ceiling_overlay_key("local-coder", "test") in result.detail

    # No overlay roots at all (a clean install): no ceilings are declared, so none applies; the read does not invent one.
    assert (
        measured_correct_ceiling("local-coder", "code_generation", environ={}) is None
    )

    # An overlay that is not a YAML mapping: the read raises.
    not_a_mapping = overlay_root(tmp_path / "not_a_mapping", NOT_A_MAPPING_OVERLAY_YAML)
    assert_ceiling_read_raises(
        NodeOverlayError,
        lambda: measured_correct_ceiling(
            "local-coder", "code_generation", environ=not_a_mapping
        ),
    )

    # An overlay that lacks input_ceilings: the read raises.
    legacy = overlay_root(tmp_path / "legacy", LEGACY_OVERLAY_YAML)
    assert_ceiling_read_raises(
        NodeOverlayError,
        lambda: measured_correct_ceiling(
            "local-coder", "code_generation", environ=legacy
        ),
    )

    # A copy of the fixture overlay: the named pair resolves, the unnamed pair is None.
    environ = overlay_root(
        tmp_path / "fixture", OVERLAY_FILE.read_text(encoding="utf-8")
    )
    assert (
        measured_correct_ceiling("local-coder", "code_generation", environ=environ)
        == 8169
    )
    assert measured_correct_ceiling("local-coder", "test", environ=environ) is None


def test_golden_chain() -> None:
    """Positive control: unmutated chain is green."""
    run_golden_chain_input_ceiling()


def test_error_chain(tmp_path: Path) -> None:
    """Positive control: unmutated error chain is green."""
    run_error_chain_input_ceiling(tmp_path)


def test_mutation_fixed_ceiling_for_every_pair(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Mutation: the handler answers 8169 for every pair, named or not."""
    module = importlib.import_module(
        "omnimarket.nodes.node_delegation_code_budget_compute.handlers.handler_delegation_code_budget"
    )

    def patched_handle(self, request: ModelCodeBudgetRequest) -> ModelCodeBudgetResult:
        return ModelCodeBudgetResult(
            operation="input_ceiling",
            task_class=request.task_class,
            call_budget_s=None,
            source="overlay",
            refusal=None,
            completed_samples=0,
            detail="",
            backend=request.backend,
            input_ceiling_tokens=8169,
            overlay_key=input_ceiling_overlay_key(
                request.backend or "", request.task_class
            ),
        )

    monkeypatch.setattr(module.HandlerDelegationCodeBudget, "handle", patched_handle)
    with pytest.raises(AssertionError):
        run_golden_chain_input_ceiling()


def test_mutation_unnamed_pair_answered(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Mutation: an unnamed pair is answered a default instead of refused."""
    module = importlib.import_module(
        "omnimarket.nodes.node_delegation_code_budget_compute.handlers.handler_delegation_code_budget"
    )
    original_handle = module.HandlerDelegationCodeBudget.handle

    def patched_handle(self, request: ModelCodeBudgetRequest) -> ModelCodeBudgetResult:
        result = original_handle(self, request)
        if result.refusal is EnumCodeBudgetRefusal.UNNAMED_INPUT_CEILING:
            return result.model_copy(
                update={"refusal": None, "input_ceiling_tokens": 4096}
            )
        return result

    monkeypatch.setattr(module.HandlerDelegationCodeBudget, "handle", patched_handle)
    with pytest.raises(AssertionError):
        run_error_chain_input_ceiling(tmp_path)


def test_mutation_default_overlay_fallback(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Mutation: an absent overlay falls back to a packaged default ceiling."""
    routing = importlib.import_module(
        "omnimarket.handlers.routing.measured_correct_ceiling"
    )
    default_overlay = ModelCodeBudgetOverlay(
        class_budgets_s={"code_generation": 480.0},
        sizing_margin=1.5,
        round_up_to_s=60.0,
        min_completed_samples=3,
        ceiling_s=600.0,
        input_ceilings={"local-coder": {"code_generation": 8169}},
    )

    def patched_load(
        overlay_id: str,
        model: type[ModelCodeBudgetOverlay],
        environ: Mapping[str, str] | None = None,
        **kwargs: Any,
    ) -> ModelCodeBudgetOverlay:
        return default_overlay

    monkeypatch.setattr(routing, "load_structured_overlay", patched_load)
    with pytest.raises(AssertionError):
        run_error_chain_input_ceiling(tmp_path)


NAMED_MUTATIONS: dict[str, tuple[str, str]] = {
    "fixed-ceiling-every-pair": (
        "golden",
        "test_mutation_fixed_ceiling_for_every_pair",
    ),
    "unnamed-pair-answered": ("error", "test_mutation_unnamed_pair_answered"),
    "default-overlay-fallback": ("error", "test_mutation_default_overlay_fallback"),
}


def test_named_mutations_match_module() -> None:
    """Verify NAMED_MUTATIONS matches the test functions defined in the module."""
    defined_tests = {
        name
        for name, obj in vars(sys.modules[__name__]).items()
        if name.startswith("test_mutation_") and callable(obj)
    }
    expected_tests = {test_name for _, test_name in NAMED_MUTATIONS.values()}
    assert defined_tests == expected_tests
