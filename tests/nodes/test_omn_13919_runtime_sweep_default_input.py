# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""OMN-13919 — runtime_sweep default input wiring + zero-entity hard fail.

Regression class: ``onex skill runtime_sweep`` dispatched the pure handler
with only ``{scope, dry_run}``, so every skill run reported ``status=no_input``
with zero contracts/topics/workflows checked while the dispatch receipt still
said ``status=success`` / ``exit_code=0`` — a vacuous false-green (second
observation of the OMN-13715/OMN-13708 class).

The fix has two halves, and these tests pin both:

1. **Default input wiring** — a request carrying no entities resolves the
   default contract set by walking ``$OMNI_HOME`` (same collection as the
   ``__main__`` CLI harness), so the no-args skill path checks real entities.
2. **Zero-entity hard fail** — a run that still checks zero entities RAISES
   instead of returning a result, making the vacuous pass unrepresentable at
   the dispatch layer (which maps any returned result to success).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from omnimarket.nodes.node_runtime_sweep.handlers.handler_runtime_sweep import (
    ModelContractInput,
    NodeRuntimeSweep,
    RuntimeSweepRequest,
)

# ---------------------------------------------------------------------------
# Fixture: a minimal $OMNI_HOME tree with contract.yaml files under nodes/
# ---------------------------------------------------------------------------

_CONTRACT_ALPHA = """\
name: node_alpha
description: A real description for the alpha fixture node.
handler:
  module: repoa.nodes.node_alpha.handlers.handler_alpha
  class: HandlerAlpha
event_bus:
  publish_topics:
    - onex.evt.fixture.alpha-done.v1
  subscribe_topics:
    - onex.cmd.fixture.alpha-start.v1
"""

_CONTRACT_DASH = """\
name: node_dash
description: A real description for the omnidash fixture node.
event_bus:
  publish_topics:
    - onex.evt.fixture.dash-done.v1
  subscribe_topics:
    - onex.evt.fixture.alpha-done.v1
"""


def _write_contract(root: Path, repo: str, node: str, body: str) -> None:
    node_dir = root / repo / "src" / repo / "nodes" / node
    node_dir.mkdir(parents=True)
    (node_dir / "contract.yaml").write_text(body)


@pytest.fixture
def fixture_omni_home(tmp_path: Path) -> Path:
    """A fake $OMNI_HOME with two repos, each holding one node contract."""
    _write_contract(tmp_path, "repoa", "node_alpha", _CONTRACT_ALPHA)
    _write_contract(tmp_path, "omnidash", "node_dash", _CONTRACT_DASH)
    return tmp_path


# ---------------------------------------------------------------------------
# 1. Default input wiring
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_empty_request_self_collects_default_input(
    monkeypatch: pytest.MonkeyPatch, fixture_omni_home: Path
) -> None:
    """No-input request (the skill dispatch shape) must check real entities."""
    monkeypatch.setenv("OMNI_HOME", str(fixture_omni_home))
    result = NodeRuntimeSweep().handle(RuntimeSweepRequest())

    assert result.contracts_checked == 2
    assert result.topics_checked > 0
    assert result.status in ("clean", "findings")
    assert result.status != "no_input"


@pytest.mark.unit
def test_skill_dispatch_payload_shape_checks_entities(
    monkeypatch: pytest.MonkeyPatch, fixture_omni_home: Path
) -> None:
    """The exact ``onex skill runtime_sweep`` payload must not be vacuous.

    The dispatch shim sends only ``{"dry_run": false}`` (plus ``scope`` when
    given) — this is the invocation that produced the OMN-13919 defect
    evidence. It must now resolve the default entity set.
    """
    monkeypatch.setenv("OMNI_HOME", str(fixture_omni_home))
    request = RuntimeSweepRequest.model_validate({"dry_run": False})
    result = NodeRuntimeSweep().handle(request)

    assert result.contracts_checked > 0
    assert result.topics_checked > 0


@pytest.mark.unit
def test_scope_omnidash_only_limits_default_collection(
    monkeypatch: pytest.MonkeyPatch, fixture_omni_home: Path
) -> None:
    """scope=omnidash-only restricts the default walk to the omnidash repo."""
    monkeypatch.setenv("OMNI_HOME", str(fixture_omni_home))
    result = NodeRuntimeSweep().handle(RuntimeSweepRequest(scope="omnidash-only"))

    assert result.contracts_checked == 1


@pytest.mark.unit
def test_explicit_input_bypasses_default_collection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Caller-supplied entities are used as-is; no $OMNI_HOME required."""
    monkeypatch.delenv("OMNI_HOME", raising=False)
    result = NodeRuntimeSweep().handle(
        RuntimeSweepRequest(
            contracts=[
                ModelContractInput(
                    node_name="node_explicit",
                    description="A real description for the explicit node.",
                    publish_topics=["onex.evt.fixture.explicit.v1"],
                    subscribe_topics=["onex.evt.fixture.explicit.v1"],
                )
            ]
        )
    )
    assert result.contracts_checked == 1
    assert result.topics_checked == 1


# ---------------------------------------------------------------------------
# 2. Zero-entity hard fail — the vacuous pass must be unrepresentable
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_zero_entities_raises_never_passes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Default collection over an empty tree checks nothing ⇒ hard failure."""
    monkeypatch.setenv("OMNI_HOME", str(tmp_path))
    with pytest.raises(ValueError, match="zero entities"):
        NodeRuntimeSweep().handle(RuntimeSweepRequest())


@pytest.mark.unit
def test_missing_omni_home_raises_never_silently_defaults(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No entities and no $OMNI_HOME ⇒ fail fast, never an empty default."""
    monkeypatch.delenv("OMNI_HOME", raising=False)
    with pytest.raises(ValueError, match="OMNI_HOME"):
        NodeRuntimeSweep().handle(RuntimeSweepRequest())


@pytest.mark.unit
def test_no_input_status_is_gone() -> None:
    """The reportable ``no_input`` status must not resurface in the handler.

    A returned ``no_input`` result maps to exit_code=0/status=success at the
    dispatch layer — the exact false-green this ticket closes. The only
    acceptable zero-entity behavior is an exception.
    """
    import inspect

    from omnimarket.nodes.node_runtime_sweep.handlers import handler_runtime_sweep

    source = inspect.getsource(handler_runtime_sweep.NodeRuntimeSweep.handle)
    assert 'status = "no_input"' not in source
