# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""One timer tick of the delegation check probe, the unit the host installer renders (OMN-26043).

The unit passes ``--omni-home`` and ``--state-dir``. A tick reads the node's overlay and the settings store's responder row,
runs each check the overlay names once, puts each run record on the bus (the contract's result topic), appends it to the
run history under the state directory and writes ``<state-dir>/last-run.json``.

Exit codes: 0 every check PASSed and its record was emitted; 1 a run concluded failure (FAIL, NOT_MEASURED,
PRECONDITION-ABSENT or ERROR): the record is still emitted and the unit shows failed; 2 the overlay or the responder row
cannot be loaded; 3 a record was not accepted by the bus. The evidence file is written for every code.

``--check <id>`` runs that one check (the build checks node runs the tick once per check). In that mode the exit code is the
one the build checks node's executor reads: 0 PASS, 1 only a FAIL (the product failed a clause), and 4 for a run that could
not measure the check (NOT_MEASURED, PRECONDITION-ABSENT or ERROR), which reads unavailable there and is never a regression.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import faulthandler
import json
import sys
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, get_args

import yaml
from omnibase_core.errors.model_onex_error import ModelOnexError
from pydantic import BaseModel

from omnimarket.handlers.bus_binding.declared_broker_publisher import (
    DeclaredBrokerPublisher,
)
from omnimarket.handlers.node_overlay_reader import (
    NodeOverlayError,
    NodeOverlayNotFoundError,
    load_structured_overlay,
)
from omnimarket.handlers.overlay_store.reader_store import (
    OverlayAbsentFromStoreError,
    StoreOverlayReader,
)
from omnimarket.models.overlay_documents.model_delegation_check_responders_overlay import (
    ModelDelegationCheckRespondersOverlay,
)
from omnimarket.nodes.node_delegation_check_probe_effect.handlers.adapter_clean_install_machine import (
    CleanInstallCustomerMachine,
    GitCloneSurfaceSources,
)
from omnimarket.nodes.node_delegation_check_probe_effect.handlers.adapter_delegation_check import (
    HttpResponderReader,
)
from omnimarket.nodes.node_delegation_check_probe_effect.handlers.adapter_gateway import (
    GATEWAY_OVERLAY_ID,
    DevTenantCredential,
    HttpGatewayCaller,
    load_gateway_row,
)
from omnimarket.nodes.node_delegation_check_probe_effect.handlers.gateway_checks import (
    CUSTOMER_LOCAL_CHECKS,
    GATEWAY_CHECKS,
)
from omnimarket.nodes.node_delegation_check_probe_effect.handlers.handler_delegation_check_probe import (
    NODE,
    HandlerDelegationCheckProbe,
    contract_topics,
)
from omnimarket.nodes.node_delegation_check_probe_effect.handlers.handler_regression_ticket import (
    BusTicketWriter,
    HandlerRegressionTicket,
    JsonRegressionStateStore,
    ProtocolTicketWriter,
    failing_step_of,
)
from omnimarket.nodes.node_delegation_check_probe_effect.models.model_delegation_check_probe import (
    CheckId,
    EnumCheckVerdict,
    ModelChainSource,
    ModelDelegationCheckProbeOverlay,
    ModelDelegationCheckRequest,
    ModelDelegationCheckResult,
    gateway_leg_verdict,
)
from omnimarket.nodes.node_delegation_check_probe_effect.models.model_regression_ticket import (
    ModelRegressionRunReading,
    ModelRegressionTicketRequest,
)
from omnimarket.nodes.node_delegation_check_probe_effect.protocols.protocol_delegation_check import (
    ProtocolCheckResultPublisher,
    ProtocolCustomerMachine,
    ProtocolGatewayClient,
)
from omnimarket.nodes.node_overlay_store_read_effect.handlers.handler_overlay_store_read import (
    OverlayStaleError,
)
from omnimarket.nodes.node_overlay_store_read_effect.runtime_overlay_store_read import (
    read_overlay,
)

ProtocolStoreDocumentReader = Callable[[str], dict[str, Any] | None]

EVIDENCE_FILE = "last-run.json"
OVERLAY_FILE_ENV = "ONEX_DELEGATION_CHECK_PROBE_OVERLAY"
RESPONDERS_OVERLAY_ID = "delegation_check_responders"
RESPONDERS_FILE_ENV = "ONEX_DELEGATION_CHECK_RESPONDERS_OVERLAY"
EXIT_FAILED = 1
EXIT_OVERLAY = 2
EXIT_NOT_PUBLISHED = 3
EXIT_NOT_MEASURED = 4


def read_store_document(overlay_id: str) -> dict[str, Any] | None:
    """The settings store reader's document for ``overlay_id``, or None when the store serves none on this host.

    The reader is the store's own (node_overlay_store_read_effect): the tick never names a snapshot directory, so a unit that
    does not carry the snapshot root on its overlay roots still reads the store (OMN-26642, measured on h051).
    """
    try:
        return dict(read_overlay(overlay_id).document)
    except OverlayStaleError:
        raise
    except NodeOverlayError:
        # No usable snapshot, or an overlay this host's store does not keep (the probe's own overlay is a file overlay on h051).
        return None


def _settings[ModelT: BaseModel](
    overlay_id: str,
    model: type[ModelT],
    reader: ProtocolStoreDocumentReader,
    *,
    file_env: str,
) -> ModelT:
    """The store reader's document first; the overlay file chain answers only when the store serves no document."""
    document = reader(overlay_id)
    return load_structured_overlay(
        overlay_id, model, explicit=document, file_env=file_env
    )


def tick_deadline_s() -> float:
    contract = yaml.safe_load(
        (Path(__file__).parent / "contract.yaml").read_text(encoding="utf-8")
    )
    return float(contract["descriptor"]["timeout_ms"]) / 1000


def evidence_for(result: ModelDelegationCheckResult) -> dict[str, Any]:
    return {
        "check": result.check_id,
        "run_id": result.run_id,
        "event": result.event,
        "status": result.status,
        "conclusion": result.conclusion,
        "verdict": result.verdict.value,
        "cause_class": result.cause_class,
        "chosen_tier": result.chosen_tier,
        "chosen_responder": result.chosen_responder,
        "served_model": result.served_model,
        "chain": [f"{a.base_url or a.tier}:{a.outcome}" for a in result.chain],
        "failed_clauses": [c.name for c in result.clauses if not c.ok],
        "absent": [p.name for p in result.preconditions if not p.present],
        "emitted": result.emitted,
        "error": result.error,
        "runner": result.runner,
        "gateway_url": result.gateway_url,
        "tenant_slug": result.tenant_slug,
        "chain_source": (
            result.chain_source.model_dump(mode="json")
            if result.chain_source is not None
            else None
        ),
    }


def _read_store_row(
    reader: StoreOverlayReader,
) -> tuple[ModelDelegationCheckRespondersOverlay, ModelChainSource]:
    """The responders row through the injected store reader, with the record's own source_ref and loaded_at."""
    record = asyncio.run(reader.read_record(RESPONDERS_OVERLAY_ID))
    document = record.get("document")
    row = ModelDelegationCheckRespondersOverlay.model_validate(document)
    loaded = record.get("loaded_at")
    return row, ModelChainSource(
        source="store",
        source_ref=str(record.get("source_ref") or RESPONDERS_OVERLAY_ID),
        loaded_at=datetime.fromisoformat(str(loaded)) if loaded else None,
        rows=row.responders,
    )


def _document_reader(record_reader: StoreOverlayReader) -> ProtocolStoreDocumentReader:
    """The record reader as a document reader: the record's document, or None for an overlay the store does not keep."""

    def read(overlay_id: str) -> dict[str, Any] | None:
        try:
            record = asyncio.run(record_reader.read_record(overlay_id))
        except OverlayAbsentFromStoreError:
            return None
        document = record["document"]
        return dict(document) if isinstance(document, dict) else None

    return read


def _write_atomically(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    held = path.with_suffix(path.suffix + ".tmp")
    held.write_text(text, encoding="utf-8")
    held.replace(path)


def _whole_check_leg(result: ModelDelegationCheckResult) -> str:
    """A gateway-only check has no machine half: its whole verdict is its leg (PASS, FAIL; anything else is unmeasured)."""
    if result.verdict is EnumCheckVerdict.PASS:
        return "pass"
    if result.verdict is EnumCheckVerdict.FAIL:
        return "fail"
    return "unmeasured"


def append_history(
    path: Path, results: list[ModelDelegationCheckResult], keep: int
) -> None:
    """One JSON line per run (the run in ``ModelMvpRun`` shape plus check and verdict), newest last, trimmed to ``keep``.

    Every line carries ``gateway_leg`` (pass, fail or unmeasured): for a customer-local check the verdict of its gateway clauses
    alone, for a gateway-only check (C16, C30 ...) its whole verdict. The beta board reads that field in place of a cloud job run.
    """
    lines: list[str] = []
    with contextlib.suppress(OSError):
        lines = path.read_text(encoding="utf-8").splitlines()
    new = [
        json.dumps(
            {
                **r.mvp_run(),
                "check": r.check_id,
                "verdict": r.verdict.value,
                "gateway_leg": (
                    gateway_leg_verdict(r.clauses)
                    if r.check_id in CUSTOMER_LOCAL_CHECKS
                    else _whole_check_leg(r)
                ),
            },
            sort_keys=True,
        )
        for r in results
    ]
    _write_atomically(path, "\n".join([*lines, *new][-keep:]) + "\n")


def main(
    argv: list[str] | None = None,
    *,
    machine: ProtocolCustomerMachine | None = None,
    publisher: ProtocolCheckResultPublisher | None = None,
    now: datetime | None = None,
    gateway: ProtocolGatewayClient | None = None,
    store_reader: ProtocolStoreDocumentReader | StoreOverlayReader | None = None,
    ticket_writer: ProtocolTicketWriter | None = None,
) -> int:
    parser = argparse.ArgumentParser(prog=f"{NODE}.tick", description=__doc__)
    parser.add_argument("--omni-home", dest="registry_root", required=True, type=Path)
    parser.add_argument("--state-dir", required=True, type=Path)
    parser.add_argument("--trigger", choices=("schedule", "manual"), default="schedule")
    parser.add_argument("--check", choices=get_args(CheckId), default=None)
    args = parser.parse_args(argv)
    faulthandler.dump_traceback_later(tick_deadline_s(), exit=True)
    stamp = now or datetime.now(UTC)
    reader: ProtocolStoreDocumentReader
    if isinstance(store_reader, StoreOverlayReader):
        reader = _document_reader(store_reader)
    else:
        reader = store_reader or read_store_document
    evidence_path = args.state_dir / EVIDENCE_FILE
    try:
        overlay = _settings(
            NODE, ModelDelegationCheckProbeOverlay, reader, file_env=OVERLAY_FILE_ENV
        )
        if overlay.result_topic not in contract_topics():
            raise ValueError(
                f"result_topic {overlay.result_topic!r} is not a topic the contract declares"
            )
        # The responder chain is the settings store's row. With none served the two checks that send a task through the
        # chain read PRECONDITION-ABSENT and C17, which reads no responder, still grades; a row that is served but broken
        # ends the tick like any other unreadable overlay.
        row: ModelDelegationCheckRespondersOverlay | None = None
        chain_source: ModelChainSource | None = None
        if isinstance(store_reader, StoreOverlayReader):
            # The record reader is the source: no file is consulted for the chain.
            try:
                row, chain_source = _read_store_row(store_reader)
            except OverlayAbsentFromStoreError:
                row = None
        else:
            try:
                document = reader(RESPONDERS_OVERLAY_ID)
                row = load_structured_overlay(
                    RESPONDERS_OVERLAY_ID,
                    ModelDelegationCheckRespondersOverlay,
                    explicit=document,
                    file_env=RESPONDERS_FILE_ENV,
                )
                chain_source = ModelChainSource(
                    source="store" if document is not None else "overlay",
                    source_ref=(
                        f"store:{RESPONDERS_OVERLAY_ID}"
                        if document is not None
                        else f"overlay:{RESPONDERS_OVERLAY_ID}"
                    ),
                    rows=row.responders,
                )
            except NodeOverlayNotFoundError:
                row = None
    except (NodeOverlayError, ModelOnexError, ValueError, OSError) as exc:
        faulthandler.cancel_dump_traceback_later()
        reading = {
            "node": NODE,
            "ran_at": stamp.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "status": "overlay_unreadable",
            "error": f"{type(exc).__name__}: {exc}"[-2000:],
        }
        _write_atomically(evidence_path, json.dumps(reading, sort_keys=True) + "\n")
        sys.stderr.write(f"{NODE}: {reading['error']}\n")
        return EXIT_OVERLAY
    wanted = (args.check,) if args.check else overlay.checks
    if gateway is None and any(c in GATEWAY_CHECKS for c in wanted):
        # The gateway address and the test tenant are the settings store's row; with none served the gateway checks read
        # PRECONDITION-ABSENT. A row that is served but broken ends the tick like any other unreadable overlay.
        try:
            gateway_row = load_gateway_row(explicit=reader(GATEWAY_OVERLAY_ID))
            gateway = HttpGatewayCaller(
                gateway_row,
                DevTenantCredential(secret_name=gateway_row.secret_name),
                overlay.responder_timeout_s,
            )
        except NodeOverlayNotFoundError:
            gateway = None
        except (NodeOverlayError, ModelOnexError, ValueError, OSError) as exc:
            faulthandler.cancel_dump_traceback_later()
            reading = {
                "node": NODE,
                "ran_at": stamp.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "status": "gateway_row_unreadable",
                "error": f"{type(exc).__name__}: {exc}"[-2000:],
            }
            _write_atomically(evidence_path, json.dumps(reading, sort_keys=True) + "\n")
            sys.stderr.write(f"{NODE}: {reading['error']}\n")
            return EXIT_OVERLAY
    handler = HandlerDelegationCheckProbe(
        responders=row.responders if row is not None else None,
        reader=HttpResponderReader(overlay.responder_timeout_s),
        machine=machine
        or CleanInstallCustomerMachine(
            sources=GitCloneSurfaceSources(args.registry_root)
        ),
        publisher=publisher or DeclaredBrokerPublisher(NODE, flat=True),
        topic=overlay.result_topic,
        gateway=gateway,
        chain_source=chain_source,
    )
    results: list[ModelDelegationCheckResult] = []
    try:
        for check in wanted:
            results.append(
                handler.handle(
                    ModelDelegationCheckRequest(
                        correlation_id=f"{check.lower()}-probe-{uuid.uuid4().hex[:12]}",
                        check_id=check,
                        trigger=args.trigger,
                    )
                )
            )
    finally:
        faulthandler.cancel_dump_traceback_later()
    append_history(args.state_dir / overlay.history_file, results, overlay.history_keep)
    tickets: list[dict[str, str]] = []
    # Only a scheduled run counts: a manual run is someone probing, not the check's own record.
    if overlay.regression_ticket is not None and args.trigger == "schedule":
        from omnimarket.nodes.node_create_ticket.handlers.core_ticket_guard import (
            core_lane_intake_policy,
        )

        settings = overlay.regression_ticket
        notice = HandlerRegressionTicket(
            store=JsonRegressionStateStore(args.state_dir / settings.state_file),
            writer=ticket_writer or BusTicketWriter(settings.lane),
            parent=settings.parent,
            stamp=f"{core_lane_intake_policy().stamp_prefix} {settings.lane}",
            repo=settings.repo,
        )
        for r in results:
            outcome = notice.handle(
                ModelRegressionTicketRequest(
                    reading=ModelRegressionRunReading(
                        check_id=r.check_id,
                        run_id=r.run_id,
                        verdict=r.verdict,
                        failing_step=failing_step_of(
                            [(c.name, c.ok) for c in r.clauses], r.error
                        ),
                    )
                )
            )
            tickets.append(
                {
                    "check": outcome.check_id,
                    "action": outcome.action.value,
                    "ticket": outcome.ticket_id,
                    "detail": outcome.detail,
                }
            )
    readings = {
        "node": NODE,
        "ran_at": stamp.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "runs": [evidence_for(r) for r in results],
        **({"regression_tickets": tickets} if tickets else {}),
    }
    _write_atomically(evidence_path, json.dumps(readings, sort_keys=True) + "\n")
    sys.stdout.write(json.dumps(readings, sort_keys=True) + "\n")
    if not all(r.emitted for r in results):
        return EXIT_NOT_PUBLISHED
    if args.check and any(
        r.verdict
        in (
            EnumCheckVerdict.NOT_MEASURED,
            EnumCheckVerdict.PRECONDITION_ABSENT,
            EnumCheckVerdict.ERROR,
        )
        for r in results
    ):
        return EXIT_NOT_MEASURED
    return 0 if all(r.conclusion == "success" for r in results) else EXIT_FAILED


__all__ = [
    "EVIDENCE_FILE",
    "EXIT_NOT_MEASURED",
    "append_history",
    "evidence_for",
    "main",
]

if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
