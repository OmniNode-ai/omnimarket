"""Architectural acceptance for the pure fold and dispatch writer boundary."""

from __future__ import annotations

import ast
import importlib
from pathlib import Path

from omnimarket.nodes.node_projection_work_ledger.handlers.handler_work_ledger_projection import (
    HandlerProjectionWorkLedger,
    WorkLedgerProjectionWriter,
)


def test_pure_projection_handler_does_not_import_or_reference_event_envelopes() -> None:
    handler_module = importlib.import_module(HandlerProjectionWorkLedger.__module__)
    assert handler_module.__file__ is not None
    module = ast.parse(Path(handler_module.__file__).read_text(encoding="utf-8"))
    imported_names = {
        alias.name
        for node in ast.walk(module)
        if isinstance(node, (ast.Import, ast.ImportFrom))
        for alias in node.names
    }
    assert "ModelEventEnvelope" not in imported_names
    assert all(
        not isinstance(node, ast.Name) or node.id != "ModelEventEnvelope"
        for node in ast.walk(module)
    )


def test_work_ledger_projection_writer_declares_runtime_dispatch_constant() -> None:
    assert WorkLedgerProjectionWriter.onex_runtime_inprocess_dispatch is True
