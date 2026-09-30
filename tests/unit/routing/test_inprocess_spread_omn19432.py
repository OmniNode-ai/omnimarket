# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-19432: the in-process (bus-less) delegate path spreads like the bus path.

The routing reducer's ``delta`` (bus path) passes the correlation id as the
spread key, so a spread-placed backend shares its rung's first-choice traffic
(OMN-19215 AC4). The in-process path (``onex delegate``) resolves its INITIAL
and escalation backends through ``backend_id_for_tier``, which passed no key, so
every unpinned run took the first rung and the second host idled. Measured
2026-09-30: 14 unpinned in-process runs, all on .201, none on .202.

What must hold, and what each test pins:

* ``backend_id_for_tier`` with a key picks one member of the group, stably per
  key, and both members take a real share; without a key it keeps its ordered
  answer (the positive control);
* the in-process port derives that key from the run's correlation id, so 200
  unpinned dispatch resolutions split across both hosts;
* a caller pin still bypasses the spread entirely.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Generator
from pathlib import Path
from uuid import NAMESPACE_DNS, UUID, uuid5

import pytest

from omnimarket.nodes.node_delegate_skill_orchestrator.ports import (
    port_local_delegation_dispatch as port_mod,
)
from omnimarket.nodes.node_delegate_skill_orchestrator.ports.port_local_delegation_dispatch import (
    LocalDelegationDispatchPort,
)
from omnimarket.nodes.node_delegation_routing_reducer.handlers import (
    handler_delegation_routing as routing,
)
from tests.unit.routing.test_same_model_spread_omn19215 import (
    _PEER_URL,
    _RUNG_URL,
    _bifrost_yaml,
    _bind,
    _reset,
)

pytestmark = pytest.mark.unit

_KEYS: tuple[UUID, ...] = tuple(
    uuid5(NAMESPACE_DNS, f"omn-19432-inprocess-{i}") for i in range(200)
)


def _resolvable_bifrost_yaml() -> str:
    """The shared fixture plus the token/timeout budgets the port's resolver requires."""
    return _bifrost_yaml("spread").replace(
        "    provider: local\n",
        "    provider: local\n    max_tokens: 4096\n    timeout_ms: 30000\n",
    )


@pytest.fixture
def _spread(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Generator[None, None, None]:
    _bind(tmp_path, monkeypatch, _resolvable_bifrost_yaml())
    yield
    _reset()


def _port(tmp_path: Path) -> LocalDelegationDispatchPort:
    return LocalDelegationDispatchPort(
        effect_handler=lambda _request: pytest.fail("no LLM call in a resolution test"),
        evidence_db_path=tmp_path / "d.sqlite",
        effect_process_boundary=False,
    )


@pytest.mark.usefixtures("_spread")
def test_backend_id_for_tier_without_a_key_keeps_its_ordered_answer() -> None:
    assert routing.backend_id_for_tier("local", "document") == "local-heavy-reasoning"


@pytest.mark.usefixtures("_spread")
def test_backend_id_for_tier_with_a_key_spreads_stably_across_both_hosts() -> None:
    picks = [
        routing.backend_id_for_tier("local", "document", spread_key=str(key))
        for key in _KEYS
    ]
    assert picks == [
        routing.backend_id_for_tier("local", "document", spread_key=str(key))
        for key in _KEYS
    ]
    by_ref = Counter(picks)
    assert set(by_ref) == {"local-heavy-reasoning", "local-omnipc2-chat"}
    assert min(by_ref.values()) >= 70, by_ref


@pytest.mark.usefixtures("_spread")
@pytest.mark.parametrize("task_type", ["document", "code_generation"])
def test_twenty_unpinned_inprocess_resolutions_split_across_both_hosts(
    tmp_path: Path, task_type: str
) -> None:
    port = _port(tmp_path)
    resolved = [
        port._resolve_initial_backend(task_type, spread_key=str(key))
        for key in _KEYS[:20]
    ]
    by_ref = Counter(backend.backend_id for backend in resolved)
    rung = "local-heavy-reasoning" if task_type == "document" else "local-coder"
    assert set(by_ref) == {rung, "local-omnipc2-chat"}, by_ref
    for backend in resolved:
        expected = (
            _PEER_URL if backend.backend_id == "local-omnipc2-chat" else _RUNG_URL
        )
        assert backend.endpoint_ref == expected


@pytest.mark.usefixtures("_spread")
def test_a_caller_pin_bypasses_the_spread(tmp_path: Path) -> None:
    port = _port(tmp_path)
    ids = {
        port._resolve_initial_backend(
            "document", backend_id="local-heavy-reasoning", spread_key=str(key)
        ).backend_id
        for key in _KEYS[:30]
    }
    assert ids == {"local-heavy-reasoning"}


def test_dispatch_threads_the_correlation_id_as_the_spread_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[str | None] = []
    correlation_id = _KEYS[0]

    def _stop(self: object, task_type: str, **kwargs: object) -> object:
        seen.append(kwargs.get("spread_key"))  # type: ignore[arg-type]
        raise RuntimeError("stop after the initial resolution")

    monkeypatch.setattr(LocalDelegationDispatchPort, "_resolve_initial_backend", _stop)
    import asyncio

    with pytest.raises(RuntimeError, match="stop after"):
        asyncio.run(
            _port(tmp_path).dispatch(
                prompt="p",
                task_type="document",
                correlation_id=correlation_id,
                max_tokens=16,
                source_file_path=None,
                source_session_id=None,
                wait=True,
                execution_timeout_seconds=240,
                terminal_delivery_margin_seconds=60,
                quality_contract_mode="extend_task_class",
                acceptance_criteria=(),
                tenant_id=None,
            )
        )
    assert seen == [str(correlation_id)]
    assert port_mod is not None
