# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The time behavior of the federation edge decision: freshness and no clock.

The probe's max age passes exactly: a probe one second older refuses
``acl-probe-stale``, and so does a probe dated after ``now``. An absent
``now`` refuses ``now-absent`` while a probe is present, and
``acl-not-measured`` wins when the probe is absent too, because the probe is
read before the freshness check. The decision module reads no clock: its
source never references ``datetime.now``, ``datetime.utcnow``, ``time.time``,
``time.monotonic`` or ``date.today``, and with every clock name patched in
the handler module's namespace -- and the time module's own functions --
to raise, the handler still answers the same decision. The time is the
input, not the host: the same request with ``now`` moved 25 hours later
flips a pass to ``acl-probe-stale``.
"""

from __future__ import annotations

import ast
import base64
import time
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import yaml

from omnimarket.models.federation_egress.enum_egress_gate import (
    EnumEgressOutcome,
)
from omnimarket.models.federation_egress.model_egress_gate import (
    ModelEgressField,
    ModelWireHeader,
)
from omnimarket.nodes.node_federation_edge_decision_compute.handlers import (
    handler_federation_edge_decision as edge_module,
)
from omnimarket.nodes.node_federation_edge_decision_compute.handlers.handler_federation_edge_decision import (
    HandlerFederationEdgeDecision,
)
from omnimarket.nodes.node_federation_edge_decision_compute.models.enum_edge_decision import (
    EnumEdgeRefusal,
)
from omnimarket.nodes.node_federation_edge_decision_compute.models.model_edge_decision import (
    ModelAclProbeRecord,
    ModelAclProbeStep,
    ModelEdgeDecision,
    ModelEdgeDecisionRequest,
    ModelLaneDeclaration,
    ModelRecordedEvent,
    ModelRegistryTopicEntry,
)
from omnimarket.nodes.node_federation_edge_decision_compute.models.model_edge_overlay import (
    ModelEdgeOverlayFile,
)

pytestmark = pytest.mark.unit

OVERLAY_EXAMPLE = (
    Path(__file__).resolve().parents[4]
    / "src/omnimarket/nodes/node_federation_edge_decision_compute/overlay.example.yaml"
)

# The fixed decision moment; the handler reads no clock, so ``now`` is input.
NOW = datetime(2026, 11, 14, 12, 0, 0, tzinfo=UTC)

# One fixed uuid string: the recorded event's command_id and key bytes.
COMMAND_ID = "0f7e1a9c-5b2d-4c8e-a1d3-9f6b2e7c4a50"

# The message-id value: a uuid derived from COMMAND_ID, so it fits the
# overlay's ``^[0-9a-f-]{36}$`` header rule and never equals the key text.
MESSAGE_ID = str(uuid.uuid5(uuid.NAMESPACE_URL, COMMAND_ID))

# The connection's mechanism, as the lane declaration declares it.
MECHANISM = "example-mechanism"

# The clock references the handler source must never make: every one of
# them would read the host the decision is made on.
CLOCK_REFERENCES = (
    "datetime.now",
    "datetime.utcnow",
    "time.time",
    "time.monotonic",
    "date.today",
)

_OVERLAY: ModelEdgeOverlayFile | None = None


def _overlay() -> ModelEdgeOverlayFile:
    """The example overlay, loaded once and shared (frozen, so immutable)."""
    global _OVERLAY
    if _OVERLAY is None:
        document = yaml.safe_load(OVERLAY_EXAMPLE.read_text(encoding="utf-8"))
        _OVERLAY = ModelEdgeOverlayFile.model_validate(document)
    return _OVERLAY


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def _value_bytes(command_id: str) -> bytes:
    """The event's raw value bytes: one JSON object naming the command."""
    return b'{"command_id":"' + command_id.encode("ascii") + b'"}'


def _event(**overrides: Any) -> ModelRecordedEvent:
    """The native unstamped recording of one event on the allowed topic."""
    direction = _overlay().direction
    values: dict[str, Any] = {
        "topic": direction.allowed_topic,
        "key_b64": _b64(COMMAND_ID.encode("ascii")),
        "value_b64": _b64(_value_bytes(COMMAND_ID)),
        "headers": (
            ModelWireHeader(name=direction.message_id_header, value=MESSAGE_ID),
        ),
    }
    values.update(overrides)
    return ModelRecordedEvent(**values)


def _steps() -> tuple[ModelAclProbeStep, ...]:
    """The five probe letters: a b allowed, c d e denied, each as expected."""
    return (
        ModelAclProbeStep(step="a", expected="allowed", observed="allowed"),
        ModelAclProbeStep(step="b", expected="allowed", observed="allowed"),
        ModelAclProbeStep(step="c", expected="denied", observed="denied"),
        ModelAclProbeStep(step="d", expected="denied", observed="denied"),
        ModelAclProbeStep(step="e", expected="denied", observed="denied"),
    )


def _probe(**overrides: Any) -> ModelAclProbeRecord:
    """This direction's fresh probe: a b allowed, c d e denied, 60s old."""
    to = _overlay().direction.to
    values: dict[str, Any] = {
        "probe_ref": to.acl_probe_ref,
        "lane": to.lane,
        "principal_ref": _overlay().binding.principal_ref,
        "recorded_at": NOW - timedelta(seconds=60),
        "steps": _steps(),
    }
    values.update(overrides)
    return ModelAclProbeRecord(**values)


def _registry_entry(**overrides: Any) -> ModelRegistryTopicEntry:
    """The allowed topic's registry entry: internal, closed one-field shape."""
    direction = _overlay().direction
    values: dict[str, Any] = {
        "topic": direction.allowed_topic,
        "classes": ("internal",),
        "atomic_group": False,
        "fields": (
            ModelEgressField(
                name=direction.key_binding,
                kind="string",
                required=True,
                pattern=direction.key_pattern,
                max_length=36,
            ),
        ),
    }
    values.update(overrides)
    return ModelRegistryTopicEntry(**values)


def _lane_declaration(**overrides: Any) -> ModelLaneDeclaration:
    """The destination lane as the deployment declares it."""
    values: dict[str, Any] = {
        "lane": _overlay().direction.to.lane,
        "auth_mechanism": MECHANISM,
        "granted_write_principals": (_overlay().binding.principal_ref,),
    }
    values.update(overrides)
    return ModelLaneDeclaration(**values)


def _request(**overrides: Any) -> ModelEdgeDecisionRequest:
    """The one passing request, with whole-field ``overrides`` applied."""
    values: dict[str, Any] = {
        "recording": (_event(),),
        "overlay": _overlay(),
        "authenticated_identity": _overlay().binding.principal_ref,
        "connection_mechanism": MECHANISM,
        "lane_declaration": _lane_declaration(),
        "registry_entry": _registry_entry(),
        "acl_probe": _probe(),
        "now": NOW,
    }
    values.update(overrides)
    # model_construct: a pydantic after-validator re-runs on an overlay instance nested in the request, so a mutated
    # overlay could only reach the handler through it; every value here is already typed.
    return ModelEdgeDecisionRequest.model_construct(**values)


def _handler() -> HandlerFederationEdgeDecision:
    return HandlerFederationEdgeDecision()


def _refuses(reason: EnumEdgeRefusal, **overrides: Any) -> ModelEdgeDecision:
    """The positive control first, then the refusal: the unmutated request
    passes, and the one built with ``overrides`` refuses with exactly
    ``reason`` and carries no record."""
    control = _handler().handle(_request())
    assert control.outcome is EnumEgressOutcome.PASS
    assert control.record_out is not None
    decision = _handler().handle(_request(**overrides))
    assert decision.outcome is EnumEgressOutcome.REFUSE
    assert decision.reason is reason
    assert decision.record_out is None
    return decision


def _raise_clock_call(*args: Any, **kwargs: Any) -> Any:
    """Stand-in for a clock function: any call is a clock read."""
    raise AssertionError("the decision module read the host clock")


class _PoisonedTime:
    """A stand-in for the ``time`` module whose clock functions raise."""

    @staticmethod
    def time() -> Any:
        raise AssertionError("the decision module read time.time")

    @staticmethod
    def monotonic() -> Any:
        raise AssertionError("the decision module read time.monotonic")


class _PoisonedDatetime(datetime):
    """A stand-in for the ``datetime`` name whose clock constructors raise."""

    @classmethod
    def now(cls) -> Any:
        raise AssertionError("the decision module read datetime.now")

    @classmethod
    def utcnow(cls) -> Any:
        raise AssertionError("the decision module read datetime.utcnow")


def _patch_the_clock_to_raise(monkeypatch: pytest.MonkeyPatch) -> None:
    """Patch every clock name the handler could resolve to raise: the names
    in the handler module's own namespace -- where a stray ``time.time`` or
    ``datetime.now`` in the module source would resolve -- and the time
    module's own functions, where a real module reference would land."""
    # The handler module's namespace: the handler imports no clock, so the
    # patch creates the names there (and would replace them where they
    # existed).
    monkeypatch.setattr(edge_module, "time", _PoisonedTime(), raising=False)
    monkeypatch.setattr(edge_module, "datetime", _PoisonedDatetime, raising=False)
    monkeypatch.setattr(time, "time", _raise_clock_call)
    monkeypatch.setattr(time, "monotonic", _raise_clock_call)


def _dotted_references(source: str) -> set[str]:
    """Every dotted reference the module's syntax tree makes that is rooted
    at a name: ``request.now``, ``datetime.now``, and their kin."""
    references: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Attribute):
            parts = [node.attr]
            base = node.value
            while isinstance(base, ast.Attribute):
                parts.append(base.attr)
                base = base.value
            if isinstance(base, ast.Name):
                references.add(".".join([base.id, *reversed(parts)]))
    return references


class TestTheMaxAgePassesExactly:
    def test_a_probe_exactly_max_age_old_passes(self) -> None:
        max_age = _overlay().direction.to.acl_probe_max_age_seconds
        decision = _handler().handle(
            _request(acl_probe=_probe(recorded_at=NOW - timedelta(seconds=max_age)))
        )
        assert decision.outcome is EnumEgressOutcome.PASS
        assert decision.reason is None
        assert decision.record_out is not None


class TestAclProbeStale:
    def test_a_probe_one_second_past_its_max_age_refuses(self) -> None:
        max_age = _overlay().direction.to.acl_probe_max_age_seconds
        _refuses(
            EnumEdgeRefusal.ACL_PROBE_STALE,
            acl_probe=_probe(recorded_at=NOW - timedelta(seconds=max_age + 1)),
        )

    def test_a_probe_dated_after_now_refuses(self) -> None:
        _refuses(
            EnumEdgeRefusal.ACL_PROBE_STALE,
            acl_probe=_probe(recorded_at=NOW + timedelta(seconds=1)),
        )


class TestNowAbsent:
    def test_no_now_with_a_probe_present_refuses_now_absent(self) -> None:
        _refuses(EnumEdgeRefusal.NOW_ABSENT, now=None)

    def test_acl_not_measured_wins_when_the_probe_is_absent_too(self) -> None:
        _refuses(EnumEdgeRefusal.ACL_NOT_MEASURED, acl_probe=None, now=None)


class TestTheDecisionReadsNoClock:
    def test_the_handler_source_never_references_the_host_clock(self) -> None:
        source = Path(edge_module.__file__).read_text(encoding="utf-8")
        references = _dotted_references(source)
        hits = [
            reference
            for reference in sorted(references)
            for clock in CLOCK_REFERENCES
            if reference == clock or reference.startswith(clock + ".")
        ]
        assert hits == []

    def test_with_every_clock_name_patched_to_raise_the_decision_is_unchanged(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        request = _request()
        plain = _handler().handle(request)
        assert plain.outcome is EnumEgressOutcome.PASS
        _patch_the_clock_to_raise(monkeypatch)
        decision = _handler().handle(request)
        assert decision.outcome is EnumEgressOutcome.PASS
        assert decision.model_dump_json() == plain.model_dump_json()


class TestTheTimeIsTheInput:
    def test_now_moved_25_hours_later_flips_pass_to_stale(self) -> None:
        assert _handler().handle(_request()).outcome is EnumEgressOutcome.PASS
        decision = _handler().handle(
            _request(now=NOW + timedelta(hours=25)),
        )
        assert decision.outcome is EnumEgressOutcome.REFUSE
        assert decision.reason is EnumEdgeRefusal.ACL_PROBE_STALE
        assert decision.record_out is None
