# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-14538 class-fix proof: ``_resolve_status`` fail-closes on real evidence.

KNOWN DEFECT (OMN-14531 detector-shelf audit): ``_resolve_status`` returned
``recorded`` unless a runtime-SHA receipt happened to be stale. RUNTIME probe
fail + SSH-dead CONTAINER_HEALTH + KAFKA fail + empty DB tables still yielded
``status: recorded`` because the roll-up never looked at whether the surface
probes that DID run actually passed, and never required that any integration
census dimension (tickets / kafka / db / projection / golden_chains) be
declared in the first place.

This module is the MANDATORY RED proof: it drives the handler through an
EXISTS-but-WRONG scope — surface probes that actually execute (never raise,
per ``surface_probes.py``'s contract) and report ``fail``/``error`` — and
asserts the sweep does NOT report ``recorded``. A green result here would be
vacuous (nothing was scanned); the whole point is proving RED against a scope
that was genuinely scanned and genuinely broken, not merely empty.

It also proves the sibling failure mode: an empty census (zero tickets, zero
kafka/db/projection/golden_chain config) drives ``blocked`` even when the 3
baseline probes all pass, because baseline health/CI probes are not
integration evidence.

Finally it proves the GREEN control: a fully-populated, genuinely-healthy
census (every surface configured and passing) still reports ``recorded`` —
the fix must not turn the sweep permanently red.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from omnimarket.nodes.node_integration_sweep_orchestrator.handlers.handler_integration_sweep_orchestrator import (
    HandlerIntegrationSweepOrchestrator,
)
from omnimarket.nodes.node_integration_sweep_orchestrator.models.model_golden_chain_descriptor import (
    ModelGoldenChainDescriptor,
)
from omnimarket.nodes.node_integration_sweep_orchestrator.models.model_integration_sweep_orchestrator_request import (
    ModelIntegrationSweepOrchestratorRequest,
)

_PROBES_SUBPROCESS = (
    "omnimarket.nodes.node_integration_sweep_orchestrator."
    "handlers.surface_probes.subprocess.run"
)


def _dead_infra_run(argv: list[str], **_kwargs: object) -> MagicMock:
    """Simulate a fully dead runtime lane: every shelled probe fails/errors.

    - ``curl`` (RUNTIME_HEALTH) returns a nonzero exit -> probe status "fail".
    - ``ssh ... docker ps`` (CONTAINER_HEALTH) returns a nonzero exit
      (connection refused) -> probe status "fail".
    - ``gh run list`` (GITHUB_CI) returns a nonzero exit -> probe status
      "fail".
    - ``ssh ... docker exec ... rpk topic list`` (KAFKA) returns nonzero
      -> probe status "fail".
    """
    out = MagicMock()
    out.returncode = 1
    out.stdout = ""
    out.stderr = "ssh: connect to host 192.168.86.201 port 22: Connection refused"  # onex-allow-internal-ip OMN-14538 reason="mocked ssh stderr text in a unit test fixture; no live connection is made"
    return out


@pytest.mark.unit
class TestResolveStatusFailClosed:
    """Direct unit coverage of ``_resolve_status`` (no I/O)."""

    def test_stale_receipt_still_blocks(self) -> None:
        """Regression: the pre-existing stale-runtime-SHA gate still holds."""
        status = HandlerIntegrationSweepOrchestrator._resolve_status(
            dry_run=False,
            probe_count=1,
            stale_count=1,
            invalid_target_count=0,
            surface_fail_count=0,
            census_dimensions_configured=1,
        )
        assert status == "blocked"

    def test_all_dimensions_healthy_records(self) -> None:
        """GREEN control at the unit level: every scanned dimension passed."""
        status = HandlerIntegrationSweepOrchestrator._resolve_status(
            dry_run=False,
            probe_count=4,
            stale_count=0,
            invalid_target_count=0,
            surface_fail_count=0,
            census_dimensions_configured=2,
        )
        assert status == "recorded"

    def test_surface_probe_failure_blocks_even_with_zero_stale_receipts(
        self,
    ) -> None:
        """RED (unit level): the exact KNOWN DEFECT scenario.

        RUNTIME fail + SSH dead + KAFKA fail + empty tables: zero stale
        runtime-SHA receipts (``stale_count=0``) but every surface probe
        that ran failed (``surface_fail_count=4``). Before OMN-14538 this
        returned ``recorded`` because only ``stale_count`` gated the verdict.
        """
        status = HandlerIntegrationSweepOrchestrator._resolve_status(
            dry_run=False,
            probe_count=4,
            stale_count=0,
            invalid_target_count=0,
            surface_fail_count=4,
            census_dimensions_configured=1,
        )
        assert status != "recorded"
        assert status == "blocked"

    def test_empty_census_blocks_even_when_baseline_probes_pass(self) -> None:
        """RED (unit level): zero census dimensions declared.

        Only the 3 baseline health/CI probes ran (``probe_count=3``), all
        passed (``surface_fail_count=0``), but the caller declared zero
        integration census (no tickets, no kafka/db/projection/golden_chain
        config) -- ``census_dimensions_configured=0``. Baseline reachability
        is not integration evidence; the verdict must not claim ``recorded``.
        """
        status = HandlerIntegrationSweepOrchestrator._resolve_status(
            dry_run=False,
            probe_count=3,
            stale_count=0,
            invalid_target_count=0,
            surface_fail_count=0,
            census_dimensions_configured=0,
        )
        assert status != "recorded"
        assert status == "blocked"

    def test_zero_probe_count_is_still_no_input(self) -> None:
        """Regression: the OMN-13924 no_input terminal state is unchanged."""
        status = HandlerIntegrationSweepOrchestrator._resolve_status(
            dry_run=False,
            probe_count=0,
            stale_count=0,
            invalid_target_count=0,
            surface_fail_count=0,
            census_dimensions_configured=0,
        )
        assert status == "no_input"


@pytest.mark.unit
class TestHandlerEndToEndFailClosed:
    """End-to-end handler proof: real ``surface_probes`` execution path."""

    def test_red_dead_runtime_lane_does_not_report_recorded(
        self, tmp_path: Path
    ) -> None:
        """RED (end-to-end): the diagnosis's exact exists-but-wrong scope.

        ``run_surface_probes=True`` against default (unreachable) infra
        hosts, zero tickets, zero kafka/db/projection/golden_chain config.
        The 3 baseline probes still execute (``surface_probes.py`` never
        raises) and every one reports "fail" because the shelled
        curl/ssh/gh commands all fail. ``probe_count=3`` (non-zero, so the
        ``no_input`` gate does not fire) -- this is EXISTS-but-WRONG, not
        absence. Before OMN-14538 this returned ``status=recorded``.
        """
        with patch(_PROBES_SUBPROCESS, side_effect=_dead_infra_run):
            result = HandlerIntegrationSweepOrchestrator().handle(
                ModelIntegrationSweepOrchestratorRequest(
                    scope="explicit",
                    tickets=[],
                    artifact_root=str(tmp_path),
                    artifact_date="2026-07-13",
                    run_surface_probes=True,
                )
            )

        assert result.status != "recorded", (
            "a dead runtime lane with every scanned probe failing must never "
            "report 'recorded'"
        )
        assert result.details["surface_probe_count"] == "3"
        assert int(result.details["surface_probe_failures"]) >= 1
        assert result.details["scanned_count"] == "3"

    def test_red_populated_census_but_every_probe_fails(self, tmp_path: Path) -> None:
        """RED (end-to-end): a fully-configured census that is genuinely down.

        Isolates the surface-fail mechanism from the census-gap mechanism —
        every dimension is declared (kafka, db, projection, golden_chain)
        so ``census_dimensions_configured`` is satisfied, but the runtime
        lane is dead so every probe reports "fail"/"error".
        """
        with patch(_PROBES_SUBPROCESS, side_effect=_dead_infra_run):
            result = HandlerIntegrationSweepOrchestrator().handle(
                ModelIntegrationSweepOrchestratorRequest(
                    scope="explicit",
                    tickets=[],
                    artifact_root=str(tmp_path),
                    artifact_date="2026-07-13",
                    run_surface_probes=True,
                    kafka_topics=["onex.evt.omnimarket.integration-sweep.v1"],
                    kafka_consumer_groups=["omnimarket-integration-sweep"],
                    db_tables=["llm_routing_decisions"],
                    projection_topics=[
                        "onex.evt.omnimarket.integration-sweep-completed.v1"
                    ],
                    golden_chains=[
                        ModelGoldenChainDescriptor(
                            chain_name="routing",
                            command_topic="onex.cmd.omnimarket.integration-sweep.v1",
                            consumer_group="omnimarket-integration-sweep",
                            tail_database="omnidash_analytics",
                            tail_table="llm_routing_decisions",
                        )
                    ],
                )
            )

        assert result.status == "blocked"
        assert result.details["census_dimensions_configured"] != "0"
        assert int(result.details["surface_probe_failures"]) > 0

    def test_green_fully_healthy_populated_scope_records(self, tmp_path: Path) -> None:
        """GREEN control (end-to-end): a genuinely-healthy populated scope.

        Every probe (RUNTIME_HEALTH, CONTAINER_HEALTH, GITHUB_CI, KAFKA, DB,
        PROJECTION, GOLDEN_CHAIN) is exercised and passes; every census
        dimension is declared. The fix must not turn a genuinely healthy
        sweep permanently red.
        """

        def _healthy_run(argv: list[str], **_kwargs: object) -> MagicMock:
            joined = " ".join(argv)
            out = MagicMock()
            out.returncode = 0
            out.stderr = ""
            if argv and argv[0] == "curl":
                out.stdout = "200" if "-w" in argv else '{"status": "ok"}'
            elif "docker ps" in joined:
                out.stdout = "web\tUp 3 hours\n"
            elif "gh" in argv[:1]:
                out.stdout = (
                    '[{"conclusion": "success", "name": "ci", "status": "completed"}]'
                )
            elif "rpk topic list" in joined:
                out.stdout = "NAME\nonex.evt.omnimarket.integration-sweep.v1\n"
            elif "rpk group list" in joined:
                out.stdout = (
                    "BROKER GROUP STATE\n0 omnimarket-integration-sweep Stable\n"
                )
            elif "to_regclass" in joined:
                out.stdout = "public.llm_routing_decisions\n"
            elif "count(*)" in joined:
                out.stdout = "5\n"
            else:
                out.stdout = ""
            return out

        with patch(_PROBES_SUBPROCESS, side_effect=_healthy_run):
            result = HandlerIntegrationSweepOrchestrator().handle(
                ModelIntegrationSweepOrchestratorRequest(
                    scope="explicit",
                    tickets=[],
                    artifact_root=str(tmp_path),
                    artifact_date="2026-07-13",
                    run_surface_probes=True,
                    kafka_topics=["onex.evt.omnimarket.integration-sweep.v1"],
                    kafka_consumer_groups=["omnimarket-integration-sweep"],
                    db_tables=["llm_routing_decisions"],
                    projection_topics=[
                        "onex.evt.omnimarket.integration-sweep-completed.v1"
                    ],
                    golden_chains=[
                        ModelGoldenChainDescriptor(
                            chain_name="routing",
                            command_topic="onex.evt.omnimarket.integration-sweep.v1",
                            consumer_group="omnimarket-integration-sweep",
                            tail_database="omnidash_analytics",
                            tail_table="llm_routing_decisions",
                        )
                    ],
                )
            )

        assert result.status == "recorded", (
            f"expected recorded on a genuinely healthy populated scope, got "
            f"{result.status!r} with surfaces={result.surfaces!r}"
        )
        assert result.details["surface_probe_failures"] == "0"
        assert result.details["census_dimensions_configured"] != "0"
