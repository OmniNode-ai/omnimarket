# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The real seams run against a recording runner: argument vectors, secrets, pruning, the report row."""

from __future__ import annotations

import plistlib
import subprocess
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml

from omnimarket.nodes.node_work_ledger_copy_effect import schedule
from omnimarket.nodes.node_work_ledger_copy_effect.adapters.adapters_real import (
    AppendReporter,
    PostgresSource,
    SshStore,
    expand_home,
    file_facts,
    read_dotenv_key,
)
from omnimarket.nodes.node_work_ledger_copy_effect.models import (
    EnumCopyOutcome,
    ModelCopyOverlay,
    ModelCopyResult,
)
from omnimarket.nodes.node_work_ledger_copy_effect.protocols import CopyStepError

pytestmark = pytest.mark.unit
_NODE = (
    Path(__file__).resolve().parents[4]
    / "src/omnimarket/nodes/node_work_ledger_copy_effect"
)
_SECRET = "s3cret-value-for-test"


def _overlay() -> ModelCopyOverlay:
    return ModelCopyOverlay.model_validate(
        yaml.safe_load((_NODE / "overlay.example.yaml").read_text())
    )


class Recorder:
    def __init__(self, stdout: str = "", returncode: int = 0) -> None:
        self.calls: list[tuple[list[str], Mapping[str, str] | None]] = []
        self.stdout = stdout
        self.returncode = returncode

    def __call__(
        self, argv: Sequence[str], env: Mapping[str, str] | None, timeout: int
    ) -> subprocess.CompletedProcess[str]:
        self.calls.append((list(argv), env))
        return subprocess.CompletedProcess(
            list(argv), self.returncode, self.stdout, "boom\nmore"
        )


def test_password_reaches_the_child_environment_and_never_an_argument() -> None:
    run = Recorder(stdout="2026-11-05T18:00:00.123456Z")
    source = PostgresSource(_overlay(), {"EXAMPLE_DATABASE_PASSWORD": _SECRET}, run)
    source.cutoff()
    with pytest.raises(CopyStepError):  # the recorder writes no dump file
        source.dump(Path("/nonexistent"))
    for argv, env in run.calls:
        assert all(_SECRET not in part for part in argv)
        assert env is not None
        assert env["PGPASSWORD"] == _SECRET


def test_missing_password_is_a_typed_dump_error_that_names_the_variable() -> None:
    source = PostgresSource(_overlay(), {}, Recorder())
    with pytest.raises(CopyStepError, match="EXAMPLE_DATABASE_PASSWORD"):
        source.cutoff()


def test_dotenv_key_is_read_without_exporting(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    env.write_text("OTHER=1\nEXAMPLE_DATABASE_PASSWORD='quoted'\n")
    assert read_dotenv_key(env, "EXAMPLE_DATABASE_PASSWORD") == "quoted"
    assert read_dotenv_key(env, "ABSENT") is None


def test_snapshot_keeps_whole_lines_only(tmp_path: Path) -> None:
    ledger = tmp_path / "ledger.md"
    ledger.write_bytes(b"one\ntwo\npartial-row-being-appended")
    source = PostgresSource(_overlay(), {"ONEX_LEDGER_PATH": str(ledger)}, Recorder())
    copy, facts = source.snapshot_file(tmp_path)
    assert copy.read_bytes() == b"one\ntwo\n"
    assert facts == file_facts(b"one\ntwo\n")
    assert facts.line_count == 2


def test_a_table_name_that_is_not_an_identifier_is_refused() -> None:
    overlay = _overlay()
    bad = overlay.model_copy(
        update={
            "source": overlay.source.model_copy(
                update={"rows_table": "rows; drop table x"}
            )
        }
    )
    with pytest.raises(ValueError, match="plain identifier"):
        PostgresSource(bad, {}, Recorder())


def test_prune_removes_only_run_shaped_names_beyond_the_newest_keep() -> None:
    listing = "\n".join(
        [
            "archive",
            "notes",
            "20261105T100000Z",
            "20261105T110000Z",
            "20261105T120000Z",
            "20261105T130000Z",
            "20261105T140000Z",
        ]
    )
    run = Recorder(stdout=listing)
    pruned = SshStore(_overlay(), run).prune(3)
    assert pruned == ("20261105T100000Z", "20261105T110000Z")
    removed = [argv[-1] for argv, _ in run.calls if "rm -rf" in argv[-1]]
    assert len(removed) == 2
    assert all("archive" not in text for text in removed)


def test_the_report_row_has_no_pipe_or_newline_in_its_detail() -> None:
    result = ModelCopyResult(
        run_id="20261105T180000Z",
        outcome=EnumCopyOutcome.DRILL_MISMATCH,
        detail="row count 1 | against 2\nline two",
    )
    row = AppendReporter(_overlay()).row(
        result, datetime(2026, 11, 5, 18, 1, tzinfo=UTC)
    )
    assert row.startswith(
        "2026-11-05T18:01:00Z | STATUS | lane=ledger-copy | src=ledger-copy"
    )
    assert "\n" not in row
    assert row.endswith("row count 1 against 2 line two")
    assert row.count(" | ") == 11


def test_a_failed_append_is_a_typed_report_error() -> None:
    result = ModelCopyResult(
        run_id="r", outcome=EnumCopyOutcome.COPIED_VERIFIED, detail="ok"
    )
    with pytest.raises(CopyStepError, match="ledger append exited 2"):
        AppendReporter(_overlay(), Recorder(returncode=2)).report(result)


def test_plist_runs_the_module_every_hour_at_the_contract_minute() -> None:
    plan = schedule.load_schedule()
    data = plistlib.loads(schedule.render_plist(_overlay(), plan))
    assert data["Label"] == plan["unit_name"]
    assert data["ProgramArguments"][1:] == ["-m", schedule.NODE_MODULE, "run"]
    assert len(data["StartCalendarInterval"]) == 24
    assert {entry["Minute"] for entry in data["StartCalendarInterval"]} == {
        plan["minute"]
    }
    assert data["RunAtLoad"] is False


def test_a_home_marker_resolves_at_run_time_and_a_tilde_inside_a_word_does_not() -> (
    None
):
    home = str(Path.home())
    assert expand_home("~/.local/bin/uv") == f"{home}/.local/bin/uv"
    assert expand_home("~/a:/usr/bin:~/b") == f"{home}/a:/usr/bin:{home}/b"
    assert expand_home("/opt/~backup/x") == "/opt/~backup/x"
    assert expand_home("~") == home
