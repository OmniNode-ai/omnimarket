# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-18565 / OMN-17228 AC5: the two delegation writers cannot diverge again on
what an UNATTRIBUTABLE quality verdict does.

WHY THIS FILE EXISTS. ``delegation_events`` is upserted from a quality-gate
verdict by two independent implementations:

* ``handler_delegation.DelegationProjectionRunner._project_quality_gate_result``
  -- the ASYNC standalone Kafka runner;
* ``handler_projection_delegation.HandlerProjectionDelegation
  .project_quality_gate_result`` -- the SYNC path the runtime kernel dispatches,
  and the one actually DEPLOYED as ``omnimarket-projection-delegation-writer``.

OMN-18139 made the async twin refuse an unattributable verdict, and its comment
block named this exact failure: a house-stamped verdict row "does not merely
misattribute itself, it blocks the correctly-attributed write behind it".
Nothing asserted the two paths agreed, so that fix read as a fix to both while
the deployed path kept its house fallback for a further week, and the staging
business proof stayed a coin flip. OMN-17228 AC5 asked for a test that fails
when the two writers disagree; the existing one
(``test_omn17228_sync_writer_not_null_columns``
``TestTheTwoWritersCannotDivergeAgain``) binds the COLUMN SET only and is blind
to this, the behavioural half.

WHAT IS AND IS NOT ASSERTED TO BE IDENTICAL. The INVARIANT is shared and is
asserted here: an unattributable verdict WRITES NO ROW, and neither path may
reach the house tenant to obtain one. The refusal MECHANICS deliberately differ
and are NOT asserted identical, because the two paths sit behind different
runtimes:

* the async runner owns a contract-declared DLQ, so it routes the record there
  with a typed reason and the offset advances;
* the sync path runs on the kernel seam, whose ``_is_projection_content_failure``
  classifies a tenant-authority refusal as the WRITE PATH's defect and therefore
  WITHHOLDS the offset (OMN-17379). Raising or DLQ-ing there would convert the
  loss of one redundant row into a permanently wedged partition, so it returns
  zero rows with a named ERROR log -- an outcome the kernel already logs at
  ERROR itself.

A test that demanded identical mechanics would be asserting a shape neither
runtime can honour, and the first lane to hit it would weaken the test rather
than the code. What must never differ is whether a row appears.
"""

from __future__ import annotations

import ast
import inspect
import json
import logging
import textwrap
from typing import Any
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from omnimarket.models.delegation.wire.model_quality_gate import ModelQualityGateResult
from omnimarket.nodes.node_projection_delegation.handlers import (
    handler_delegation,
    handler_projection_delegation,
)
from omnimarket.nodes.node_projection_delegation.handlers.handler_delegation import (
    DelegationProjectionRunner,
)
from omnimarket.nodes.node_projection_delegation.handlers.handler_projection_delegation import (
    TABLE,
    HandlerProjectionDelegation,
)
from omnimarket.projection.envelope import unwrap_envelope
from omnimarket.projection.protocol_database import InmemoryDatabaseAdapter
from omnimarket.projection.runner import MessageMeta

pytestmark = [pytest.mark.unit]

#: The producer-recorded envelope time. Both paths refuse an un-timed verdict
#: for an unrelated reason (OMN-15583), so it is supplied here to keep this
#: module's subject the TENANT refusal rather than the time one.
_ENVELOPE_TIMESTAMP = "2026-09-17T06:57:48+00:00"


async def _publish(topic: str, value: bytes) -> None:
    return


def _verdict_delivery(correlation_id: str) -> dict[str, Any]:
    """An unattributable quality-gate-result delivery, in the ASYNC runner's
    wire shape -- an envelope recording no ``tenant_id`` at all."""
    envelope: dict[str, Any] = {
        "payload": {
            "correlation_id": correlation_id,
            "passed": True,
            "fail_category": "pass",
            "quality_score": 1.0,
            "failure_reasons": [],
            "fallback_recommended": False,
            "score_source": "deterministic_acceptance",
            "actual_score": 1.0,
        },
        "envelope_id": str(uuid4()),
        "correlation_id": correlation_id,
        "event_type": "omnibase-infra.quality-gate-result",
        "envelope_timestamp": _ENVELOPE_TIMESTAMP,
    }
    unwrapped = unwrap_envelope(json.dumps(envelope).encode("utf-8"))
    assert unwrapped is not None
    return unwrapped


def _async_runner() -> DelegationProjectionRunner:
    runner = DelegationProjectionRunner(publish_fn=_publish)
    runner._db = AsyncMock()  # type: ignore[assignment]
    runner._db.execute = AsyncMock(return_value=[])
    return runner


def _verdict_writes(mock_db: AsyncMock) -> list[Any]:
    return [
        call
        for call in mock_db.execute.await_args_list
        if f"INSERT INTO {TABLE}" in str(call.args[0])
    ]


class TestBothWritersRefuseAnUnattributableVerdict:
    """The shared invariant. If either of these goes green-by-writing, the two
    implementations have diverged again and the deployed one is authoring a
    tenant nobody recorded."""

    @pytest.mark.asyncio
    async def test_the_async_runner_issues_no_write(self) -> None:
        runner = _async_runner()
        runner._route_malformed_to_dlq = AsyncMock(return_value=True)  # type: ignore[assignment]
        correlation_id = str(uuid4())

        await runner._project_quality_gate_result(
            _verdict_delivery(correlation_id),
            MessageMeta(partition=0, offset=324, fallback_id=correlation_id),
        )

        assert _verdict_writes(runner._db) == [], (
            "the async verdict path issued a delegation_events INSERT for a "
            "verdict recording no tenant -- the house-tenant stamp is back"
        )
        assert runner._route_malformed_to_dlq.await_count == 1

    def test_the_deployed_sync_path_writes_no_row(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        db = InmemoryDatabaseAdapter()
        correlation_id = str(uuid4())

        with caplog.at_level(logging.ERROR):
            result = HandlerProjectionDelegation().project_quality_gate_result(
                ModelQualityGateResult(
                    correlation_id=correlation_id,
                    passed=True,
                    quality_score=1.0,
                    actual_score=1.0,
                ),
                db,
                tenant_identity=None,
                event_timestamp=_ENVELOPE_TIMESTAMP,
            )

        assert result.rows_upserted == 0
        assert db.query(TABLE, {"correlation_id": correlation_id}) == [], (
            "the DEPLOYED verdict path authored a row for a verdict recording "
            "no tenant -- this is the divergence OMN-18565 closed, returned"
        )
        assert any("OMN-18565" in record.getMessage() for record in caplog.records), (
            "the refusal must name its reason; a silent drop is "
            "indistinguishable from a verdict nobody ever published"
        )

    def test_the_sync_path_still_writes_an_attributed_verdict(self) -> None:
        """Negative control on both assertions above.

        Without it, a writer that refused EVERY verdict -- including the
        attributed ones the fix exists to let through -- would satisfy this
        module completely.
        """
        db = InmemoryDatabaseAdapter()
        correlation_id = str(uuid4())

        result = HandlerProjectionDelegation().project_quality_gate_result(
            ModelQualityGateResult(
                correlation_id=correlation_id,
                passed=True,
                quality_score=1.0,
                actual_score=1.0,
            ),
            db,
            tenant_identity="omninode",
            event_timestamp=_ENVELOPE_TIMESTAMP,
        )

        assert result.rows_upserted == 1
        assert db.query(TABLE, {"correlation_id": correlation_id})


class TestNeitherVerdictPathCanReachTheHouseTenant:
    """The structural half, which catches the revert the behavioural half would
    also catch AND the next fallback shape that is not yet written.

    Read from each method's own source rather than from a comment in either,
    which is what ``TestTheTwoWritersCannotDivergeAgain`` established for the
    column set and what this extends to attribution.
    """

    #: The one canonical writer-side house-tenant expression, plus the two
    #: literal representations of that identity. Naming all three is what makes
    #: this resistant to a fallback reintroduced by a different route than the
    #: helper.
    _HOUSE_REACHES = ("house_tenant_write_stamp", "HOUSE_TENANT_UUID", "820272f9")

    @staticmethod
    def _verdict_sources() -> dict[str, str]:
        return {
            "async": inspect.getsource(
                handler_delegation.DelegationProjectionRunner._project_quality_gate_result
            ),
            "sync": inspect.getsource(
                handler_projection_delegation.HandlerProjectionDelegation.project_quality_gate_result
            ),
        }

    @staticmethod
    def _code_identifiers(source: str) -> set[str]:
        """Every NAME, ATTRIBUTE and string literal the method's CODE uses.

        Parsed rather than grepped, and that is the whole point. Both methods
        narrate this defect at length in their comments and docstrings -- the
        async twin's block is the reason the sync one was ever fixed -- so a
        text search would forbid the explanation along with the behaviour, and
        the first lane to hit it would delete the prose. An AST walk sees only
        what the code does.
        """
        tree = ast.parse(textwrap.dedent(source))
        function = tree.body[0]
        assert isinstance(function, ast.FunctionDef | ast.AsyncFunctionDef)
        # DROP THE DOCSTRING NODE, structurally. It is an ``Expr`` holding a
        # ``Constant`` and would otherwise be collected as a string literal --
        # which would put the whole prose block back under the needle and undo
        # the reason for parsing in the first place. ``ast.get_docstring``
        # normalises indentation, so comparing its RESULT against the raw
        # constant silently fails to match; removing the node does not.
        if (
            function.body
            and isinstance(function.body[0], ast.Expr)
            and isinstance(function.body[0].value, ast.Constant)
            and isinstance(function.body[0].value.value, str)
        ):
            function.body = function.body[1:]
        identifiers: set[str] = set()
        for node in ast.walk(function):
            if isinstance(node, ast.Name):
                identifiers.add(node.id)
            elif isinstance(node, ast.Attribute):
                identifiers.add(node.attr)
            elif isinstance(node, ast.Constant) and isinstance(node.value, str):
                identifiers.add(node.value)
        return identifiers

    @pytest.mark.parametrize("which", ["async", "sync"])
    def test_the_verdict_path_names_no_house_tenant_expression(
        self, which: str
    ) -> None:
        identifiers = self._code_identifiers(self._verdict_sources()[which])
        for reach in self._HOUSE_REACHES:
            offenders = [name for name in identifiers if reach in name]
            assert not offenders, (
                f"the {which} verdict path reaches the house tenant in code, "
                f"via {sorted(offenders)!r}. An unattributable verdict must "
                "author no row: stamping the house identity creates a row "
                "under a tenant that did not submit the work, and under FORCE "
                "ROW LEVEL SECURITY that row then blocks the "
                "correctly-attributed terminal behind a USING-clause refusal "
                "(OMN-18565, OMN-18139)"
            )

    def test_the_terminal_path_legitimately_does_reach_it(self) -> None:
        """Positive control on the assertion above.

        The house-tenant ruling (2026-08-02, OMN-16831 option D) is UNCHANGED
        for a terminal event: it owns the row and is its authority on every
        other column, so an unattributed terminal is still stamped with the
        house tenant EXPLICITLY, by the writer. Only a derived, partial event is
        forbidden from authoring attribution.

        Without this control, a misspelled needle above would pass on both
        paths and the module would assert nothing at all.
        """
        source = inspect.getsource(handler_projection_delegation.terminal_write_tenant)
        assert "house_tenant_write_stamp" in source, (
            "the terminal fallback no longer reaches the house tenant, so the "
            "assertion above is no longer discriminating -- either the ruling "
            "changed (update both) or this module now proves nothing"
        )
