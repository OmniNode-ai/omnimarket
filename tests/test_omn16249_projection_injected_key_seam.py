# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Cross-boundary seam tests for the runtime-injected projection keys (OMN-16249).

WHAT BROKE. A live canary submitted a real batch through the deployed gateway
(``POST /v1/workflows``, workflow_type ``hook-event-capture``) on onex-dev. The
gateway returned HTTP 202, the ``omninode-runtime-effects`` consumer received
the message (log-matched ``correlation_id``), and then
``ModelHookEventCaptureRequest`` rejected it with a pydantic ``extra_forbidden``
on a field named ``_envelope_id``. The event went to the platform DLQ and zero
rows landed, with nothing in the caller-visible 202 indicating failure.

WHERE THE SEAM ACTUALLY IS -- and where it is NOT. The gateway does not inject
``_envelope_id``; its wire envelope names that field ``envelope_id``, with no
leading underscore. The only producer of the underscore-prefixed key anywhere is
``omnibase_infra``'s projection dispatch arm
(``handler_wiring._make_projection_dispatch_callback``), which injects a set of
runtime bookkeeping keys -- ``_db``, ``_event_type``, ``_topic``, and
``_envelope_id`` -- into the plain dict it hands ``handle()``. That is a
platform convention, not a defect. The defect is that the two halves of the
convention are enumerated independently in two repos: the producer added a
fourth key; ``omnimarket``'s canonical stripper,
``omnimarket.projection.handler_shim.split_projection_input``, still enumerated
three. Anything the stripper does not know about survives into the payload and
detonates against an ``extra="forbid"`` model.

WHY IT HID FOR SO LONG. The producer injects ``_envelope_id`` only when the
dispatched envelope carries a coercible ``envelope_id``. Reducers fed by
internal events without envelope identity never saw the key, so the six
``split_projection_input`` callers looked healthy. Gateway-published envelopes
always carry one, so the first node on that path -- ``node_hook_event_capture``
-- failed on its first real message.

WHAT THESE TESTS DRIVE. Not two independent doubles. The producer side is the
REAL ``omnibase_infra`` dispatch callback, executed; the consumer side is the
REAL handler, the REAL shim, and the REAL ``extra="forbid"`` model. The injected
key set is HARVESTED from the installed producer at runtime rather than
restated here, so these tests keep testing the true seam after the producer
changes -- which is the property whose absence caused OMN-16249.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import uuid
from typing import Any
from unittest.mock import patch

import pytest
import yaml
from omnibase_core.models.contracts.subcontracts.model_db_table_declaration import (
    ModelDbTableDeclaration,
)
from omnibase_core.models.events.model_event_envelope import ModelEventEnvelope
from omnibase_infra.runtime.auto_wiring import handler_wiring
from omnibase_infra.runtime.auto_wiring.handler_wiring import (
    _make_projection_dispatch_callback,
    _resolve_projection_database_target,
)
from omnibase_infra.topology import load_topology_profile

from omnimarket.nodes.node_hook_event_capture.handlers.handler_hook_event_capture import (
    HandlerHookEventCapture,
    HookEventCaptureError,
)
from omnimarket.projection.handler_shim import (
    RUNTIME_INJECTED_KEYS,
    split_projection_input,
)
from omnimarket.projection.protocol_database import InmemoryDatabaseAdapter

_PATCH_BUILD_ADAPTER = (
    "omnibase_infra.runtime.auto_wiring.handler_wiring._build_projection_db_adapter"
)
_DSN = "postgresql://user:pass@host:5432/omnidash_analytics"

# The producer only injects the identity key when the envelope carries a
# coercible ``envelope_id``. Every gateway-published envelope does, so a
# gateway-shaped fixture is what exercises the failing path.
_TENANT_SLUG = "omninode"
_TENANT_PRINCIPAL_ID = "t-" + "1" * 32
_CORRELATION_ID = "116571c0-0000-4000-8000-000000000000"

# The DSN env vars the projection bindings resolve. These are set explicitly
# rather than inherited: ``_make_projection_dispatch_callback`` validates the
# bindings EAGERLY at construction, so a host that happens to export these
# (a developer Mac) builds the callback while a clean host (the gate runner)
# raises before the harvest can run. Depending on ambient environment would
# make this suite pass or fail on host identity rather than on the seam.
_DSN_ENV_VARS = ("OMNIDASH_ANALYTICS_DB_URL", "OMNINODE_INTERNAL_DB_URL")


@pytest.fixture(autouse=True)
def _configured_projection_dsns(monkeypatch: pytest.MonkeyPatch) -> None:
    """Model startup with configured DSNs, independent of the host."""
    for env_var in _DSN_ENV_VARS:
        monkeypatch.setenv(env_var, _DSN)


def _harvest_injected_keys_from_real_runtime() -> tuple[frozenset[str], dict[str, Any]]:
    """Run the REAL infra dispatch callback and capture what it actually injects.

    Returns the injected key set and the full ``input_data`` dict the runtime
    built, so the consumer-side tests below can be fed the genuine producer
    output instead of a hand-written approximation of it.

    The projection target is resolved from a node whose table the INSTALLED
    ``omnibase_infra`` topology already grants. That choice is deliberate and
    load-bearing: ``hook_events``' own grant landed in a later infra than the
    version this repo pins, so resolving it here would fail on a topology
    detail that has nothing to do with key injection. The target is consumed
    only by ``_build_projection_db_adapter`` (patched out below) and by DSN
    validation -- it does not reach the payload-building code under test, so
    which granted table supplies it cannot change the harvested key set.
    """
    captured: dict[str, Any] = {}

    class _KeyHarvestHandler:
        def handle(self, input_data: dict[str, object]) -> dict[str, int]:
            captured.update(input_data)
            return {"rows_upserted": 1}

    topology = load_topology_profile("local")
    target = _resolve_projection_database_target(
        [
            ModelDbTableDeclaration(
                name="dep_health_findings",
                database_ref="application",
                schema="public",
                access="write",
                role="dep_health_findings",
                migration="0001_create_dep_health_findings.sql",
            )
        ],
        topology,
    )
    topic = "onex.cmd.omnimarket.hook-event-capture-requested.v1"
    envelope = ModelEventEnvelope[object](
        payload={"probe": "value"},
        envelope_id=uuid.uuid4(),
        event_type=topic,
    )
    callback = _make_projection_dispatch_callback(
        _KeyHarvestHandler(), target, (topic,)
    )

    with patch(_PATCH_BUILD_ADAPTER, return_value=InmemoryDatabaseAdapter()):
        asyncio.run(callback(envelope))

    if not captured:
        raise AssertionError(
            "The real projection dispatch callback never invoked handle(); the "
            "harvest produced nothing, so no assertion below would be meaningful."
        )
    injected = frozenset(key for key in captured if key.startswith("_"))
    return injected, dict(captured)


def _gateway_shaped_batch() -> dict[str, object]:
    """Build the batch payload the gateway publishes for this workflow.

    Field-for-field against ``ModelHookEventCaptureRequest``'s declared set: the
    gateway's passthrough spec (``source``/``batch_sha``/``events``), the fields
    the gateway injects on every envelope (``correlation_id``/``emitted_at``/
    ``tenant_id``), and the immutable ``tenant_principal_id`` it injects for
    tenant-keyed workflows. ``event_sha`` is the sha256 over the canonical body,
    computed the way the operator-side shipper computes it, because it is the
    dedupe key rather than decoration.
    """
    body = {"hook_event_name": "PreToolUse", "tool_name": "Bash"}
    payload_json = json.dumps(body, sort_keys=True, separators=(",", ":"))
    event_sha = hashlib.sha256(payload_json.encode("utf-8")).hexdigest()
    return {
        "source": "onex-spool-ship",
        "batch_sha": hashlib.sha256(event_sha.encode("utf-8")).hexdigest(),
        "events": [
            {
                "event_type": "artifact.captured",
                "event_sha": event_sha,
                "occurred_at": "2026-08-19T05:50:00.000000Z",
                "payload_json": payload_json,
                "spool_reason": (
                    "FileNotFoundError: [Errno 2] No such file or directory"
                ),
            }
        ],
        "correlation_id": _CORRELATION_ID,
        "emitted_at": "2026-08-19T05:50:00.000000Z",
        "tenant_id": _TENANT_SLUG,
        "tenant_principal_id": _TENANT_PRINCIPAL_ID,
    }


@pytest.mark.unit
class TestInjectedKeySeamMatchesAcrossRepos:
    """The producer's key set and the consumer's stripper must stay matched."""

    def test_stripper_covers_every_key_the_real_runtime_injects(self) -> None:
        """Fail-closed drift guard: the seam's two halves, compared directly.

        This is the mechanism that makes the fix durable rather than a one-time
        patch. It executes the installed producer and compares what it actually
        injected against the frozenset the stripper actually uses. A future
        upstream key lands here as a red test in this repo's own CI, with no
        cross-repo release coordination required to detect it.
        """
        injected, _ = _harvest_injected_keys_from_real_runtime()

        missing = injected - RUNTIME_INJECTED_KEYS
        assert not missing, (
            "omnibase_infra's projection dispatch injects runtime bookkeeping "
            f"key(s) {sorted(missing)!r} that "
            "omnimarket.projection.handler_shim.RUNTIME_INJECTED_KEYS does not "
            "strip. Every projection handler with an extra='forbid' model will "
            "reject its events with extra_forbidden and the runtime will route "
            "them to the DLQ while callers keep seeing success. This is the "
            "OMN-16249 failure exactly; widen RUNTIME_INJECTED_KEYS."
        )

    def test_guard_observes_the_identity_key_that_caused_omn16249(self) -> None:
        """Pin the specific regression, so the guard above cannot pass vacuously.

        If the producer ever stops injecting ``_envelope_id``, the drift guard
        would still pass while silently no longer covering the case this ticket
        was filed for. This asserts the harvest genuinely reaches that code path.
        """
        injected, captured = _harvest_injected_keys_from_real_runtime()
        assert "_envelope_id" in injected, (
            "The harvest did not observe _envelope_id, so the drift guard is "
            "no longer exercising the OMN-16249 path."
        )
        assert isinstance(captured["_envelope_id"], uuid.UUID)

    def test_stripper_does_not_over_claim_keys_the_runtime_never_injects(self) -> None:
        """The stripper must not silently eat domain fields.

        A blanket leading-underscore strip would destroy legitimate
        underscore-aliased domain fields (e.g. ``_runtime_backend``). Keeping the
        two sets equal -- not merely a superset -- is what stops the fix for one
        drift direction from opening the other.
        """
        injected, _ = _harvest_injected_keys_from_real_runtime()
        over_claimed = RUNTIME_INJECTED_KEYS - injected
        assert not over_claimed, (
            f"RUNTIME_INJECTED_KEYS claims {sorted(over_claimed)!r}, which the "
            "real runtime does not inject. Those keys would be stripped out of "
            "domain payloads that legitimately carry them."
        )

    def test_producer_key_assignments_are_statically_discoverable(self) -> None:
        """Fail closed if the producer's shape stops being harvestable.

        The harvest is a live execution, but this repo cannot control an
        upstream refactor. If the producer moves key injection somewhere the
        single dispatch path no longer reaches, the harvest could quietly return
        a smaller set and every assertion above would pass while covering
        nothing. Cross-check against the producer's source so a shape change is
        loud rather than silent.
        """
        import inspect

        source = inspect.getsource(handler_wiring)
        statically_assigned = frozenset(
            re.findall(r'input_data\[\s*"(_[A-Za-z0-9_]+)"\s*\]\s*=', source)
        )
        assert statically_assigned, (
            "No input_data['_*'] assignments found in the installed "
            "omnibase_infra handler_wiring. The producer's injection shape "
            "changed; re-derive this seam instead of trusting the harvest."
        )
        uncovered = statically_assigned - RUNTIME_INJECTED_KEYS
        assert not uncovered, (
            f"omnibase_infra statically assigns {sorted(uncovered)!r} into the "
            "projection handle() payload, uncovered by RUNTIME_INJECTED_KEYS."
        )


@pytest.mark.unit
class TestHookEventCaptureSurvivesRealRuntimeInjection:
    """The node that failed live must now consume the real producer's output."""

    def test_handler_persists_batch_from_real_runtime_injected_payload(self) -> None:
        """End-to-end across the seam: real producer output -> real handler -> row.

        RED before the fix with the exact live symptom: ``HookEventCaptureError``
        wrapping a pydantic ``extra_forbidden`` on ``_envelope_id``.
        """
        _, captured = _harvest_injected_keys_from_real_runtime()

        db = InmemoryDatabaseAdapter()
        input_data: dict[str, object] = dict(captured)
        input_data.pop("probe", None)  # harvest fixture payload, not batch data
        input_data.update(_gateway_shaped_batch())
        input_data["_db"] = db

        result = HandlerHookEventCapture().handle(input_data)

        assert result["rows_upserted"] >= 1, (
            "The handler returned zero rows_upserted, which the runtime treats "
            "as a failed projection (no terminal event emitted)."
        )
        rows = db.query("hook_events")
        assert len(rows) == 1
        assert rows[0]["event_sha"] == _gateway_shaped_batch()["events"][0]["event_sha"]  # type: ignore[index]

    def test_identity_key_alone_no_longer_poisons_the_batch(self) -> None:
        """Isolate the regression to the one key, so a pass cannot be incidental."""
        db = InmemoryDatabaseAdapter()
        input_data: dict[str, object] = {
            "_db": db,
            "_event_type": "omnimarket.hook-event-capture-requested",
            "_topic": "onex.cmd.omnimarket.hook-event-capture-requested.v1",
            "_envelope_id": uuid.uuid4(),
            **_gateway_shaped_batch(),
        }
        result = HandlerHookEventCapture().handle(input_data)
        assert result["rows_upserted"] >= 1

    def test_genuinely_malformed_batch_still_raises(self) -> None:
        """The fix must not have widened the model's tolerance for real garbage.

        Guards the forbidden shape this ticket ruled out: had the fix loosened
        ``ModelHookEventCaptureRequest`` to ``extra="allow"``, this would pass
        an unknown field through instead of rejecting it.
        """
        db = InmemoryDatabaseAdapter()
        input_data: dict[str, object] = {
            "_db": db,
            "_event_type": "omnimarket.hook-event-capture-requested",
            "_topic": "onex.cmd.omnimarket.hook-event-capture-requested.v1",
            "_envelope_id": uuid.uuid4(),
            "definitely_not_a_declared_field": "boom",
            **_gateway_shaped_batch(),
        }
        with pytest.raises(HookEventCaptureError, match="malformed"):
            HandlerHookEventCapture().handle(input_data)


@pytest.mark.unit
class TestSplitProjectionInputIdentityKey:
    """Direct unit coverage of the stripper's new behaviour."""

    def test_identity_key_is_stripped_from_model_payload(self) -> None:
        db = InmemoryDatabaseAdapter()
        envelope_id = uuid.uuid4()
        _, payload, meta = split_projection_input(
            {
                "_db": db,
                "_event_type": "e",
                "_topic": "t",
                "_envelope_id": envelope_id,
                "run_id": "r1",
            }
        )
        assert payload == {"run_id": "r1"}
        assert meta["_envelope_id"] == envelope_id

    def test_identity_key_is_surfaced_for_idempotency_use(self) -> None:
        """The runtime injects it so reducers can key idempotency on it.

        Dropping it on the floor would strip the capability the producer's own
        comment says the key exists to provide.
        """
        db = InmemoryDatabaseAdapter()
        envelope_id = uuid.uuid4()
        _, _, meta = split_projection_input(
            {"_db": db, "_envelope_id": envelope_id, "run_id": "r1"}
        )
        assert meta == {"_envelope_id": envelope_id}

    def test_absent_identity_key_yields_no_meta_entry(self) -> None:
        """Injection is conditional upstream; absence must stay absence."""
        db = InmemoryDatabaseAdapter()
        _, payload, meta = split_projection_input({"_db": db, "run_id": "r1"})
        assert payload == {"run_id": "r1"}
        assert "_envelope_id" not in meta

    def test_underscore_aliased_domain_field_still_survives(self) -> None:
        """Re-pins OMN-13825's constraint against the widened key set."""
        db = InmemoryDatabaseAdapter()
        _, payload, _meta = split_projection_input(
            {
                "_db": db,
                "_envelope_id": uuid.uuid4(),
                "_runtime_backend": "sandbox",
                "correlation_id": "c1",
            }
        )
        assert payload["_runtime_backend"] == "sandbox"
        assert payload["correlation_id"] == "c1"


@pytest.mark.unit
def test_hook_event_capture_contract_still_declares_projection_dispatch() -> None:
    """The whole analysis rests on this node routing through the projection arm.

    ``handler_wiring`` branches to the injecting callback purely on the presence
    of ``db_io.db_tables``. If that block were ever removed, this node would be
    dispatched a validated model instead of a dict and every assertion above
    would be reasoning about a path it no longer takes.
    """
    contract = yaml.safe_load(
        (
            __import__("pathlib").Path(__file__).resolve().parents[1]
            / "src/omnimarket/nodes/node_hook_event_capture/contract.yaml"
        ).read_text()
    )
    tables = (contract.get("db_io") or {}).get("db_tables") or []
    assert [table["name"] for table in tables] == ["hook_events"]
