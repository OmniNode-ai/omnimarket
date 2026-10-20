# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The lab SLI pack runs only with an explicit deployment opt-in (OMN-17427)."""

from collections.abc import Callable, Mapping
from functools import partial
from pathlib import Path
from unittest.mock import Mock

import pytest

from omnimarket.handlers.lab_sli_pack_gate import (
    LabSliPackDisabledError,
    lab_sli_pack_enabled,
)
from omnimarket.handlers.node_overlay_reader import NodeOverlayError
from omnimarket.models.lab_sli import ModelLabSliAlertRequest, ModelLabSliBoardRequest
from omnimarket.nodes.node_lab_sli_alert_effect.handlers.handler_lab_sli_alert import (
    HandlerLabSliAlert,
)
from omnimarket.nodes.node_lab_sli_board_compute.handlers.handler_lab_sli_board import (
    HandlerLabSliBoard,
)
from omnimarket.nodes.node_lab_sli_collect_effect.handlers.handler_lab_sli_collect import (
    HandlerLabSliCollect,
)
from omnimarket.nodes.node_lab_sli_compute.handlers.handler_lab_sli_evaluate import (
    HandlerLabSliEvaluate,
)
from tests.nodes.node_lab_sli_compute.test_golden_chain_lab_sli import _night
from tests.nodes.node_lab_sli_compute.test_golden_chain_lab_sli_collect import (
    FakeProbe,
    _darwin,
    _request,
)

pytestmark = pytest.mark.unit

NODES = (
    "node_lab_sli_collect_effect",
    "node_lab_sli_compute",
    "node_lab_sli_alert_effect",
    "node_lab_sli_board_compute",
)


def _overlay_environ(root: Path, content: str) -> dict[str, str]:
    overlay = root / "lab_sli_pack" / "overlay.yaml"
    overlay.parent.mkdir(parents=True)
    overlay.write_text(content, encoding="utf-8")
    return {"ONEX_SKILL_OVERLAY_ROOTS": str(root)}


@pytest.fixture
def enabled_environ(tmp_path: Path) -> dict[str, str]:
    return _overlay_environ(tmp_path / "explicit-enabled", "status: enabled\n")


def _invocation(
    node_name: str,
    environ: Mapping[str, str],
    enabled_environ: Mapping[str, str],
) -> tuple[Callable[[], object], Mock, list[tuple[str, bytes]]]:
    """Construct with the supplied environment, reusing the golden chain inputs."""
    probe = Mock(spec=FakeProbe, wraps=_darwin())
    sent: list[tuple[str, bytes]] = []
    if node_name == "node_lab_sli_collect_effect":
        collect = HandlerLabSliCollect(probe=probe, environ=environ)
        return partial(collect.handle, _request()), probe, sent
    if node_name == "node_lab_sli_compute":
        evaluate = HandlerLabSliEvaluate(environ=environ)
        return partial(evaluate.handle, _night()), probe, sent
    # Prepare downstream inputs with a separate, explicitly enabled environment.
    evaluation = HandlerLabSliEvaluate(environ=enabled_environ).handle(_night())
    if node_name == "node_lab_sli_alert_effect":
        alert = HandlerLabSliAlert(
            event_publisher=lambda topic, body: sent.append((topic, body)),
            channel_resolver=lambda: "C-TEST",
            environ=environ,
        )
        return (
            partial(alert.handle, ModelLabSliAlertRequest(evaluation=evaluation)),
            probe,
            sent,
        )
    assert node_name == "node_lab_sli_board_compute"
    board = HandlerLabSliBoard(environ=environ)
    return (
        partial(board.handle, ModelLabSliBoardRequest(evaluation=evaluation)),
        probe,
        sent,
    )


@pytest.mark.parametrize("node_name", NODES)
@pytest.mark.parametrize("content", [None, "{}\n", "status: disabled\n"])
def test_disabled_pack_constructs_then_refuses_before_any_probe_or_push(
    node_name: str,
    content: str | None,
    tmp_path: Path,
    enabled_environ: dict[str, str],
) -> None:
    environ = (
        {"ONEX_SKILL_OVERLAY_ROOTS": ""}
        if content is None
        else _overlay_environ(tmp_path / "explicit-disabled", content)
    )
    invoke, probe, sent = _invocation(node_name, environ, enabled_environ)
    assert probe.mock_calls == []
    assert sent == []
    with pytest.raises(LabSliPackDisabledError) as raised:
        invoke()
    message = str(raised.value)
    for expected in (
        node_name,
        "off by default",
        "status: enabled",
        "<root>/lab_sli_pack/overlay.yaml",
        "ONEX_SKILL_OVERLAY_ROOTS",
    ):
        assert expected in message
    assert probe.mock_calls == []
    assert sent == []


@pytest.mark.parametrize("node_name", NODES)
def test_enabled_pack_runs(node_name: str, enabled_environ: dict[str, str]) -> None:
    assert lab_sli_pack_enabled(enabled_environ) is True
    invoke, probe, sent = _invocation(node_name, enabled_environ, enabled_environ)
    assert invoke() is not None
    if node_name == "node_lab_sli_collect_effect":
        assert probe.mock_calls
    if node_name == "node_lab_sli_alert_effect":
        assert len(sent) == 2


@pytest.mark.parametrize("node_name", NODES)
@pytest.mark.parametrize("status", ["yes", "true", "'yes'"])
def test_broken_status_is_an_overlay_error(
    node_name: str,
    status: str,
    tmp_path: Path,
    enabled_environ: dict[str, str],
) -> None:
    environ = _overlay_environ(tmp_path / "explicit-broken", f"status: {status}\n")
    invoke, probe, sent = _invocation(node_name, environ, enabled_environ)
    with pytest.raises(NodeOverlayError, match="lab_sli_pack") as raised:
        invoke()
    assert not isinstance(raised.value, LabSliPackDisabledError)
    if status == "'yes'":
        assert "enabled" in str(raised.value)
        assert "disabled" in str(raised.value)
    assert probe.mock_calls == []
    assert sent == []


@pytest.mark.parametrize("environ", [{}, {"ONEX_SKILL_OVERLAY_ROOTS": ""}])
def test_no_overlay_is_off(environ: dict[str, str]) -> None:
    assert lab_sli_pack_enabled(environ) is False


@pytest.mark.parametrize("content", ["{}\n", "status: disabled\n"])
def test_missing_status_or_disabled_is_off(content: str, tmp_path: Path) -> None:
    environ = _overlay_environ(tmp_path / "explicit-off", content)
    assert lab_sli_pack_enabled(environ) is False
