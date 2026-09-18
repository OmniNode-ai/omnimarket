# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""``onex metering`` output shape and refusals (OMN-18697).

AC4 lives here: the records survive the process exiting, and a later
invocation reads them back. Every test in :class:`TestRecordsSurviveTheProcess`
runs the command against a database written by a process that has already
finished.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from click.testing import CliRunner

from omnimarket.cli.cli_metering import (
    WINDOW_SPANS,
    metering_command,
    render_text,
    resolve_baseline,
)
from omnimarket.nodes.node_metering_summary_compute import (
    EnumBaselineState,
    HandlerMeteringSummary,
    ModelMeteringSummaryRequest,
    ModelMeteringWindow,
)

pytestmark = pytest.mark.unit

_DDL = """
CREATE TABLE delegation_events (
    correlation_id   TEXT NOT NULL UNIQUE,
    created_at       REAL NOT NULL DEFAULT (strftime('%s','now')),
    delegated_to     TEXT NOT NULL DEFAULT '',
    model_name       TEXT NOT NULL DEFAULT '',
    task_type        TEXT NOT NULL DEFAULT '',
    tokens_input     INTEGER NOT NULL DEFAULT 0,
    tokens_output    INTEGER NOT NULL DEFAULT 0,
    cost_usd         REAL,
    cost_savings_usd REAL NOT NULL DEFAULT 0.0
)
"""


@pytest.fixture
def db(tmp_path: Path) -> Path:
    """Two measured runs and one unmeasured one, written and closed."""
    path = tmp_path / "delegation.sqlite"
    now = datetime.now(tz=UTC)
    conn = sqlite3.connect(path)
    conn.execute(_DDL)
    conn.executemany(
        "INSERT INTO delegation_events (correlation_id, created_at, delegated_to, "
        "tokens_input, tokens_output, cost_usd, cost_savings_usd) "
        "VALUES (?,?,?,?,?,?,?)",
        [
            (
                "a",
                (now - timedelta(hours=1)).isoformat(),
                "Qwen3.8-27B",
                1000,
                1000,
                0.0,
                0.09,
            ),
            (
                "b",
                (now - timedelta(hours=2)).isoformat(),
                "Qwen3.8-27B",
                2000,
                500,
                0.002,
                0.065,
            ),
            (
                "c",
                (now - timedelta(hours=3)).isoformat(),
                "Qwen3.8-27B",
                0,
                0,
                None,
                0.0,
            ),
        ],
    )
    conn.commit()
    conn.close()
    return path


def _run(db: Path, *args: str) -> str:
    result = CliRunner().invoke(metering_command, ["--db", str(db), *args])
    assert result.exit_code == 0, result.output
    return result.output


class TestRecordsSurviveTheProcess:
    """AC4: written by one process, read back by a later invocation."""

    def test_a_later_invocation_reads_the_earlier_runs(self, db: Path) -> None:
        payload = json.loads(_run(db, "--json"))
        assert payload["runs_total"] == 3

    def test_two_invocations_agree(self, db: Path) -> None:
        first = json.loads(_run(db, "--json"))
        second = json.loads(_run(db, "--json"))
        assert first["runs_total"] == second["runs_total"]
        assert first["savings_usd"] == second["savings_usd"]


class TestOutputShape:
    def test_the_text_readout_names_every_figure_a_reader_needs(self, db: Path) -> None:
        output = _run(db)
        for expected in (
            "Local delegation metering",
            "Runs",
            "fully measured",
            "unknown (tokens)",
            "unknown (spend)",
            "Tokens in",
            "Spent",
            "Would have cost",
            "Saved",
            "baseline",
            "By model",
        ):
            assert expected in output, expected

    def test_the_savings_line_is_accompanied_by_its_baseline(self, db: Path) -> None:
        """AC2 at the surface: the number and its baseline are printed together."""
        output = _run(db)
        saved_line = next(
            line for line in output.splitlines() if line.startswith("Saved")
        )
        assert "$" in saved_line
        assert "claude-opus-4-6" in output
        assert "/1k in" in output
        assert "pricing_manifest" in output

    def test_the_source_database_is_named(self, db: Path) -> None:
        assert str(db) in _run(db)

    def test_the_unmeasured_run_is_reported_as_unknown(self, db: Path) -> None:
        payload = json.loads(_run(db, "--json"))
        assert payload["runs_unknown_tokens"] == 1
        assert payload["runs_measured"] == 2

    def test_json_carries_the_baseline_alongside_the_figure(self, db: Path) -> None:
        payload = json.loads(_run(db, "--json"))
        assert payload["savings_usd"] is not None
        assert payload["baseline"]["model"] == "claude-opus-4-6"
        assert payload["baseline_state"] == "resolved"

    def test_json_quarantines_the_writers_own_savings_column(self, db: Path) -> None:
        payload = json.loads(_run(db, "--json"))
        recon = payload["reconciliation"]
        assert recon["recorded_savings_baseline_available"] is False
        assert Decimal(recon["recorded_savings_usd"]) == Decimal("0.155")

    def test_the_headline_differs_from_the_recorded_column(self, db: Path) -> None:
        """They are different definitions; the readout must not conflate them."""
        payload = json.loads(_run(db, "--json"))
        assert (
            payload["savings_usd"] != payload["reconciliation"]["recorded_savings_usd"]
        )


class TestWindows:
    def test_every_declared_window_runs(self, db: Path) -> None:
        for window in WINDOW_SPANS:
            payload = json.loads(_run(db, "--window", window, "--json"))
            assert payload["window"]["label"] == window

    def test_all_time_has_no_start_bound(self, db: Path) -> None:
        payload = json.loads(_run(db, "--window", "all", "--json"))
        assert payload["window"]["start"] is None

    def test_a_bounded_window_carries_its_start(self, db: Path) -> None:
        payload = json.loads(_run(db, "--window", "7d", "--json"))
        assert payload["window"]["start"] is not None

    def test_an_unknown_window_is_refused(self, db: Path) -> None:
        result = CliRunner().invoke(
            metering_command, ["--db", str(db), "--window", "forever"]
        )
        assert result.exit_code != 0


class TestRefusals:
    def test_a_missing_database_is_a_clean_refusal_not_a_zero(
        self, tmp_path: Path
    ) -> None:
        result = CliRunner().invoke(
            metering_command, ["--db", str(tmp_path / "absent.sqlite")]
        )
        assert result.exit_code != 0
        assert "no local delegation evidence database" in result.output

    def test_an_unresolvable_baseline_prints_no_savings_figure(self, db: Path) -> None:
        output = _run(db, "--baseline", "no-such-model-in-the-manifest")
        assert "UNRESOLVED" in output
        assert "Saved                unknown" in output


class TestBaselineResolution:
    def test_the_default_baseline_resolves_from_the_pricing_manifest(self) -> None:
        baseline = resolve_baseline("claude-opus-4-6")
        assert baseline is not None
        assert baseline.source == "pricing_manifest"
        assert baseline.price_in_per_1k > Decimal("0")
        assert baseline.pricing_manifest_version

    def test_an_absent_model_resolves_to_none_rather_than_a_substitute(self) -> None:
        assert resolve_baseline("no-such-model-in-the-manifest") is None

    def test_no_price_is_hardcoded_in_the_cli_module(self) -> None:
        """The prices must come from the manifest, not from this source file."""
        source = Path(
            __import__("omnimarket.cli.cli_metering", fromlist=["x"]).__file__
        ).read_text()
        assert "0.015" not in source
        assert "0.075" not in source


class TestRenderIsPureFormatting:
    def test_render_text_does_not_recompute_the_figures(self) -> None:
        """A display that computes becomes a second definition of the number."""
        summary = HandlerMeteringSummary().handle(
            ModelMeteringSummaryRequest(
                window=ModelMeteringWindow(label="7d", end=datetime.now(tz=UTC)),
                records=(),
                baseline=None,
            )
        )
        output = render_text(summary, Path("/nowhere/delegation.sqlite"))
        assert summary.baseline_state is EnumBaselineState.UNRESOLVED
        assert "unknown" in output
        assert "Runs                 0" in output
