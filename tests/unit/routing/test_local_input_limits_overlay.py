# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The local input limits are overlay values, with no packaged number behind them (OMN-23631).

The local rungs of the packaged ``routing_tiers.yaml`` carry no window and the
local backends of ``bifrost_delegation.yaml`` carry no grounded input budget:
a rung's window is the largest ceiling the code budget overlay names for its
backend, and a pair the overlay does not name is a typed refusal that names the
key. The failing control puts the old packaged 8192 back and shows the golden
assertion turn red. Every fixture is in memory.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from omnimarket.handlers.node_overlay_reader import NodeOverlayError
from omnimarket.handlers.routing.measured_correct_ceiling import (
    InputLimitNotDeclaredError,
    required_input_ceiling,
)
from omnimarket.models.delegation.wire import (
    ModelDelegationConfig,
    parse_delegation_config_yaml,
)
from omnimarket.nodes.node_delegation_routing_reducer.handlers import (
    handler_delegation_routing as routing,
)

pytestmark = pytest.mark.unit

_CONFIGS = Path(__file__).resolve().parents[3] / "src" / "omnimarket" / "configs"
_FIXTURE_OVERLAY = (
    Path(__file__).resolve().parents[2]
    / "nodes"
    / "node_delegation_code_budget_compute"
    / "fixtures"
    / "overlays"
    / "node_delegation_code_budget_compute"
    / "overlay.yaml"
)
_HEAVY = "local-heavy-reasoning"
_CODER = "local-coder"
_KEY = "delegation.code_budget.input_ceilings"
_SERVED_WINDOW = 65536
_OLD_PACKAGED_WINDOW = 8192
_TASK_TOKENS = 20000


def _overlay_root(tmp_path: Path, heavy_window: int | None) -> Path:
    """A root holding the fixture overlay with every reasoning class at ``heavy_window``."""
    data = yaml.safe_load(_FIXTURE_OVERLAY.read_text(encoding="utf-8"))
    if heavy_window is None:
        del data["input_ceilings"][_HEAVY]
    else:
        data["input_ceilings"][_HEAVY] = dict.fromkeys(
            data["input_ceilings"][_HEAVY], heavy_window
        )
    root = tmp_path / "overlay_roots"
    target = root / "node_delegation_code_budget_compute" / "overlay.yaml"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(yaml.safe_dump(data), encoding="utf-8")
    return root


def _parse_packaged_tiers() -> ModelDelegationConfig:
    return parse_delegation_config_yaml(
        (_CONFIGS / "routing_tiers.yaml").read_text(encoding="utf-8")
    )


def _rung(config: ModelDelegationConfig, backend: str) -> routing.ModelTierModel:
    return next(m for t in config.tiers for m in t.models if m.backend_ref == backend)


def _offered_backend(
    config: ModelDelegationConfig, task: str, tokens: int
) -> str | None:
    """The backend the local tier offers a ``task`` of ``tokens`` first, or ``None``."""
    local = next(t for t in config.tiers if t.name == "local")
    backends = {
        m.backend_ref: routing.BifrostBackendRef(
            endpoint_url=f"http://{m.backend_ref}.invalid/v1",
            model_name=f"model-{m.backend_ref}",
            timeout_ms=1000,
            max_tokens=256,
        )
        for m in local.models
    }
    selected = routing._select_model_for_task(
        local.models, task, tokens, backends, require_credential=False
    )
    return None if selected is None else selected.backend_ref


def _golden_assertion(config: ModelDelegationConfig) -> None:
    """The window is the overlay's, and a 20000-token research task is offered to the rung."""
    assert _rung(config, _HEAVY).max_context_tokens == _SERVED_WINDOW
    assert _offered_backend(config, "research", _TASK_TOKENS) == _HEAVY


def test_overlay_window_is_the_reasoning_rungs_window(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A served-window overlay lets the reasoning rung take a task the old 8192 refused."""
    monkeypatch.setenv(
        "ONEX_SKILL_OVERLAY_ROOTS", str(_overlay_root(tmp_path, _SERVED_WINDOW))
    )
    _golden_assertion(_parse_packaged_tiers())


def test_old_packaged_window_turns_the_golden_assertion_red(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Failing control: the injected break puts the packaged 8192 back on the rung."""
    monkeypatch.setenv(
        "ONEX_SKILL_OVERLAY_ROOTS", str(_overlay_root(tmp_path, _SERVED_WINDOW))
    )
    config = _parse_packaged_tiers()
    broken = config.model_copy(
        update={
            "tiers": tuple(
                tier.model_copy(
                    update={
                        "models": tuple(
                            m.model_copy(
                                update={"max_context_tokens": _OLD_PACKAGED_WINDOW}
                            )
                            if m.backend_ref == _HEAVY
                            else m
                            for m in tier.models
                        )
                    }
                )
                for tier in config.tiers
            )
        }
    )
    assert _offered_backend(broken, "research", _TASK_TOKENS) != _HEAVY
    with pytest.raises(AssertionError):
        _golden_assertion(broken)


def test_no_overlay_root_is_a_typed_refusal_naming_the_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Error chain: no overlay names the windows, so the ladder does not parse."""
    empty = tmp_path / "empty_roots"
    empty.mkdir()
    monkeypatch.setenv("ONEX_SKILL_OVERLAY_ROOTS", str(empty))
    with pytest.raises(InputLimitNotDeclaredError):
        _parse_packaged_tiers()


def test_backend_the_overlay_does_not_name_is_refused_naming_its_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ONEX_SKILL_OVERLAY_ROOTS", str(_overlay_root(tmp_path, None)))
    with pytest.raises(InputLimitNotDeclaredError, match=f"{_KEY}.{_HEAVY}"):
        _parse_packaged_tiers()


def test_unreadable_overlay_is_not_a_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "overlay_roots"
    target = root / "node_delegation_code_budget_compute" / "overlay.yaml"
    target.parent.mkdir(parents=True)
    target.write_text("- not\n- a mapping\n", encoding="utf-8")
    monkeypatch.setenv("ONEX_SKILL_OVERLAY_ROOTS", str(root))
    with pytest.raises(NodeOverlayError) as raised:
        _parse_packaged_tiers()
    assert not isinstance(raised.value, InputLimitNotDeclaredError)


def test_required_ceiling_names_the_key_of_an_unnamed_pair() -> None:
    """The fixture overlay names local-heavy-reasoning research; it does not name local-coder summarization."""
    assert required_input_ceiling(_HEAVY, "research") == _OLD_PACKAGED_WINDOW
    with pytest.raises(
        InputLimitNotDeclaredError, match=f"{_KEY}.{_CODER}.summarization"
    ):
        required_input_ceiling(_CODER, "summarization")


def test_backend_the_overlay_does_not_name_keeps_its_own_declaration() -> None:
    """A local backend of a customer's own, absent from the overlay, is not held to a house value."""
    assert required_input_ceiling("customer-local", "document") is None


def test_dispatch_budget_of_a_local_backend_is_the_overlay_ceiling(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """resolve_backend_grounding_budget reads the overlay for a local backend, and refuses an unnamed class."""
    monkeypatch.setenv(
        "ONEX_SKILL_OVERLAY_ROOTS", str(_overlay_root(tmp_path, _SERVED_WINDOW))
    )
    assert (
        routing.resolve_backend_grounding_budget(_HEAVY, "research") == _SERVED_WINDOW
    )
    with pytest.raises(
        InputLimitNotDeclaredError, match=f"{_KEY}.{_HEAVY}.code_generation"
    ):
        routing.resolve_backend_grounding_budget(_HEAVY, "code_generation")


def test_no_packaged_local_limit_remains() -> None:
    """Nothing under src/omnimarket/configs holds a local input limit."""
    tiers = yaml.safe_load(
        (_CONFIGS / "routing_tiers.yaml").read_text(encoding="utf-8")
    )
    local_rungs = [
        m
        for tier in tiers["tiers"]
        for m in tier["models"]
        if m["backend_id"] in {_CODER, _HEAVY}
    ]
    assert local_rungs
    for rung in local_rungs:
        assert "max_context_tokens" not in rung, rung
        assert "fast_path_threshold_tokens" not in rung, rung
    bifrost = yaml.safe_load(
        (_CONFIGS / "bifrost_delegation.yaml").read_text(encoding="utf-8")
    )
    local_backends = [
        b for b in bifrost["backends"] if b["backend_id"] in {_CODER, _HEAVY}
    ]
    assert len(local_backends) == 2
    for backend in local_backends:
        assert "max_grounded_input_tokens" not in backend, backend["backend_id"]
    grounding = yaml.safe_load(
        (_CONFIGS / "grounding_budget_by_task_class.yaml").read_text(encoding="utf-8")
    )
    assert grounding["backends"] == {}
