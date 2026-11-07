# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Host power effect (OMN-22906): refusal of a host with no route, both backends under one suite."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from omnimarket.handlers.node_overlay_reader import load_structured_overlay
from omnimarket.nodes.node_host_power_effect.handlers.handler_host_power import (
    HandlerHostPower,
    select_backend,
)
from omnimarket.nodes.node_host_power_effect.models.model_host_power import (
    EnumHostPowerAction,
    EnumHostPowerBackend,
    EnumHostPowerOutcome,
    EnumHostPowerRefusal,
    EnumHostPowerState,
    ModelHostPowerRequest,
    ModelHostPowerResult,
)
from omnimarket.nodes.node_host_power_effect.models.model_host_power_overlay import (
    NODE_NAME,
    ModelHostPowerOverlay,
)
from tests.nodes.node_ilo_redfish_effect.conftest import (
    build,
    consent,
    scope_for,
)

pytestmark = pytest.mark.unit

NODE_DIR = (
    Path(__file__).resolve().parents[4] / "src/omnimarket/nodes/node_host_power_effect"
)
OFF_REF = "2026-11-02T11:00:00Z"
ON_REF = "2026-11-02T11:05:00Z"
RESTART_REF = "2026-11-02T11:10:00Z"
# The iLO fixture host. Both backends serve this name so one suite runs on both.
HOST = "h9"
NO_ROUTE_HOST = "h-no-route"


def overlay_doc(backend: str, route: str) -> dict[str, object]:
    doc: dict[str, object] = {
        "backend": backend,
        "writes_need_approval": True,
        "hosts": [
            {
                "name": HOST,
                "power_route": route,
                "allowed_actions": ["status", "on", "force_off", "force_restart"],
                "initial_state": "on",
            },
            {"name": NO_ROUTE_HOST, "allowed_actions": ["status", "on"]},
        ],
    }
    if backend == "ilo_redfish":
        doc["ilo_ledger_path_env"] = "EXAMPLE_LEDGER_PATH"
    return doc


@dataclass
class Rig:
    handler: HandlerHostPower
    kind: str
    state: Callable[[], str]


def make_rig(kind: str) -> Rig:
    if kind == "in_memory":
        overlay = ModelHostPowerOverlay.model_validate(overlay_doc(kind, "simulated"))
        backend = select_backend(overlay)
        return Rig(
            HandlerHostPower(overlay, backend),
            kind,
            lambda: backend.state_of(HOST).value,  # type: ignore[attr-defined]
        )
    ilo = build(
        "gen9",
        approvals=(
            consent(scope_for(HOST, "power:force_off"), ref=OFF_REF),
            consent(scope_for(HOST, "power:on"), ref=ON_REF),
            consent(scope_for(HOST, "power:force_restart"), ref=RESTART_REF),
        ),
    )
    overlay = ModelHostPowerOverlay.model_validate(overlay_doc(kind, "ilo_redfish"))
    return Rig(
        HandlerHostPower(overlay, select_backend(overlay, ilo.handler.handle)),
        kind,
        lambda: str(
            ilo.device.resources["/redfish/v1/systems/1/"]["PowerState"]
        ).lower(),
    )


@pytest.fixture(params=["in_memory", "ilo_redfish"])
def rig(request: pytest.FixtureRequest) -> Rig:
    return make_rig(request.param)


def ask(
    action: EnumHostPowerAction, host: str = HOST, ref: str | None = None
) -> ModelHostPowerRequest:
    return ModelHostPowerRequest(
        host=host, action=action, requested_by="lane-a", approval_ref=ref
    )


def assert_no_route_refusal(result: ModelHostPowerResult) -> None:
    """The AC1 reading: a host with no declared route is refused, typed, with nothing sent."""
    assert result.outcome is EnumHostPowerOutcome.REFUSED
    assert result.refusal is EnumHostPowerRefusal.NO_POWER_ROUTE
    assert result.route is None
    assert result.state_before is EnumHostPowerState.UNKNOWN
    assert result.state_after is EnumHostPowerState.UNKNOWN


# -- AC1: the refusal -----------------------------------------------------------------------


def test_host_power_refuses_a_host_with_no_declared_route(rig: Rig) -> None:
    assert_no_route_refusal(
        rig.handler.handle(ask(EnumHostPowerAction.STATUS, NO_ROUTE_HOST))
    )


def test_host_power_refusal_reading_has_teeth_when_the_route_check_is_removed(
    rig: Rig,
) -> None:
    """Injected break: a handler that skips the route check. The reading must fail on it."""

    class SkipsRouteCheck(HandlerHostPower):
        def handle(self, request: ModelHostPowerRequest) -> ModelHostPowerResult:
            row = self._overlay.host(request.host)
            assert row is not None
            reply = self._backend.apply(request)
            return ModelHostPowerResult(
                host=request.host,
                action=request.action,
                outcome=reply.outcome,
                detail=reply.detail,
                route=row.power_route,
                state_before=reply.state_before,
                state_after=reply.state_after,
            )

    broken = SkipsRouteCheck(rig.handler._overlay, rig.handler._backend)
    with pytest.raises(AssertionError):
        assert_no_route_refusal(
            broken.handle(ask(EnumHostPowerAction.STATUS, NO_ROUTE_HOST))
        )


def test_host_power_refuses_an_unknown_host(rig: Rig) -> None:
    result = rig.handler.handle(ask(EnumHostPowerAction.STATUS, "h-not-in-table"))
    assert result.refusal is EnumHostPowerRefusal.HOST_UNKNOWN
    assert result.outcome is EnumHostPowerOutcome.REFUSED


def test_host_power_refuses_an_action_the_host_does_not_allow(rig: Rig) -> None:
    result = rig.handler.handle(
        ask(EnumHostPowerAction.POWER_BUTTON, ref="2026-11-02T11:00:00Z")
    )
    assert result.refusal is EnumHostPowerRefusal.ACTION_NOT_ALLOWED
    assert rig.state() == "on"


def test_host_power_refuses_a_write_with_no_cited_approval(rig: Rig) -> None:
    result = rig.handler.handle(ask(EnumHostPowerAction.FORCE_OFF))
    assert result.refusal is EnumHostPowerRefusal.APPROVAL_MISSING
    assert rig.state() == "on"


def test_host_power_refuses_a_route_the_backend_does_not_serve() -> None:
    overlay = ModelHostPowerOverlay.model_validate(
        overlay_doc("in_memory", "ilo_redfish")
    )
    result = HandlerHostPower(overlay, select_backend(overlay)).handle(
        ask(EnumHostPowerAction.STATUS)
    )
    assert result.refusal is EnumHostPowerRefusal.ROUTE_BACKEND_MISMATCH


# -- one suite on both backends -------------------------------------------------------------


def test_host_power_status_reads_the_state(rig: Rig) -> None:
    result = rig.handler.handle(ask(EnumHostPowerAction.STATUS))
    assert result.outcome is EnumHostPowerOutcome.OK
    assert (result.state_before, result.state_after) == (
        EnumHostPowerState.ON,
        EnumHostPowerState.ON,
    )


def test_host_power_force_off_then_on_changes_state_and_reads_it_back(rig: Rig) -> None:
    off = rig.handler.handle(ask(EnumHostPowerAction.FORCE_OFF, ref=OFF_REF))
    assert off.outcome is EnumHostPowerOutcome.OK
    assert (off.state_before, off.state_after) == (
        EnumHostPowerState.ON,
        EnumHostPowerState.OFF,
    )
    assert rig.state() == "off"
    on = rig.handler.handle(ask(EnumHostPowerAction.ON, ref=ON_REF))
    assert on.outcome is EnumHostPowerOutcome.OK
    assert (on.state_before, on.state_after) == (
        EnumHostPowerState.OFF,
        EnumHostPowerState.ON,
    )


def test_host_power_on_when_already_on_is_a_noop(rig: Rig) -> None:
    result = rig.handler.handle(ask(EnumHostPowerAction.ON, ref=ON_REF))
    assert result.outcome is EnumHostPowerOutcome.NOOP
    assert rig.state() == "on"


def test_host_power_restart_of_a_powered_off_host_does_not_report_ok(rig: Rig) -> None:
    rig.handler.handle(ask(EnumHostPowerAction.FORCE_OFF, ref=OFF_REF))
    result = rig.handler.handle(ask(EnumHostPowerAction.FORCE_RESTART, ref=RESTART_REF))
    assert result.outcome in {
        EnumHostPowerOutcome.FAILED,
        EnumHostPowerOutcome.REFUSED,
    }
    assert rig.state() == "off"


def test_host_power_ilo_refusal_is_carried_as_a_typed_backend_refusal() -> None:
    rig = make_rig("ilo_redfish")
    # A cited approval that names a different action: the iLO node refuses, the result says so.
    result = rig.handler.handle(ask(EnumHostPowerAction.FORCE_RESTART, ref=OFF_REF))
    assert result.outcome is EnumHostPowerOutcome.REFUSED
    assert result.refusal is EnumHostPowerRefusal.BACKEND_REFUSED
    assert result.detail == "approval_invalid"
    assert rig.state() == "on"


# -- overlay --------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "profile", ["overlays/in_memory.yaml", "overlays/ilo_redfish.example.yaml"]
)
def test_host_power_both_shipped_profiles_load_and_decide_the_no_route_case(
    profile: str,
) -> None:
    overlay = load_structured_overlay(
        NODE_NAME,
        ModelHostPowerOverlay,
        explicit=yaml.safe_load((NODE_DIR / profile).read_text(encoding="utf-8")),
    )
    row = next(h for h in overlay.hosts if h.power_route is None)
    assert row.allowed_actions
    assert overlay.backend in set(EnumHostPowerBackend)


def test_host_power_overlay_is_found_under_the_overlay_roots(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / NODE_NAME
    target.mkdir()
    (target / "overlay.yaml").write_text(
        (NODE_DIR / "overlays/in_memory.yaml").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    monkeypatch.setenv("ONEX_SKILL_OVERLAY_ROOTS", str(tmp_path))
    overlay = load_structured_overlay(NODE_NAME, ModelHostPowerOverlay)
    assert overlay.backend is EnumHostPowerBackend.IN_MEMORY


def test_host_power_overlay_refuses_a_missing_setting_and_a_repeated_host() -> None:
    doc = overlay_doc("in_memory", "simulated")
    del doc["writes_need_approval"]
    with pytest.raises(ValidationError):
        ModelHostPowerOverlay.model_validate(doc)
    doc = overlay_doc("in_memory", "simulated")
    doc["hosts"] = [doc["hosts"][0], doc["hosts"][0]]  # type: ignore[index]
    with pytest.raises(ValidationError):
        ModelHostPowerOverlay.model_validate(doc)
    doc = overlay_doc("ilo_redfish", "ilo_redfish")
    del doc["ilo_ledger_path_env"]
    with pytest.raises(ValidationError):
        ModelHostPowerOverlay.model_validate(doc)


def test_host_power_ilo_backend_needs_the_ilo_port() -> None:
    overlay = ModelHostPowerOverlay.model_validate(
        overlay_doc("ilo_redfish", "ilo_redfish")
    )
    with pytest.raises(ValueError, match="port"):
        select_backend(overlay)
