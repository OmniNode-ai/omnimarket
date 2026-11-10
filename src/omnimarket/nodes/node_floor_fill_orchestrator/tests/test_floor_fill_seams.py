# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The seams of node_floor_fill_orchestrator, each judged from both of its sides (OMN-22854).

A seam is a shape the orchestrator shares with another piece. Each test below reads one seam from the other side's
own code, so it fails when either side changes shape:

* process effect (``node_lane_process_effect``): the real posix runner, bound as the orchestrator's runner, starts
  one process per line with the argv the fill rendered and the key, ticket and attempt in its environment.
* lane start entry (``node_remote_lane_start_orchestrator``, the lane start nodes): the options the fill's placeholders
  name are options of the entry's own parser, and the options the entry requires that the fill cannot supply are
  pinned, so the live binding cannot be forgotten.
* state table (``floor_fill_state``, omnibase_infra migration 113): the statements the store issues name only columns
  of the migration, and the insert names every column the migration leaves without a default.
* ticket seam (T11, ``node_floor_task_ticket_effect``): the mint result states and the seam's methods are pinned; the
  other side pins the same literal in its own tests.
* overlay: the contract's keys, the config model's fields and the example overlay are one list, and the start node's
  example names a key of it.
"""

from __future__ import annotations

import contextlib
import functools
import os
import re
import signal
import sys
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any, ClassVar, get_args

import pytest
import yaml
from omnibase_spi.protocols.lane import ProtocolLaneProcessRunner

from omnimarket.handlers import floor_fill_state_store_postgres as store_module
from omnimarket.nodes.node_floor_fill_orchestrator.models import (
    EnumFloorFillCommand,
    ModelFloorFillConfig,
    ModelFloorFillStoreSettings,
)
from omnimarket.nodes.node_floor_fill_orchestrator.orchestration.ports import (
    ModelMintResult,
    ProtocolFloorTickets,
)
from omnimarket.nodes.node_floor_fill_orchestrator.orchestration.scripted_ports import (
    ScriptedStarter,
)
from omnimarket.nodes.node_floor_fill_orchestrator.tests.support import (
    harness,
    overlay,
)
from omnimarket.nodes.node_lane_process_effect.handlers.handler_posix_process import (
    HandlerPosixProcess,
)
from omnimarket.nodes.node_lane_process_effect.handlers.handler_scripted_runner import (
    HandlerScriptedRunner,
)
from omnimarket.nodes.node_remote_lane_start_orchestrator import (
    runtime_remote_lane_start,
)

pytestmark = [pytest.mark.unit]

NODES = Path(__file__).resolve().parents[2]
ORCHESTRATOR = NODES / "node_floor_fill_orchestrator"
START_NODE = NODES / "node_remote_lane_start_orchestrator"

# What the fill's placeholders name, and what it can supply to a start entry from the keys it holds.
FILL_PLACEHOLDERS = ("lane", "key", "ticket", "attempt")
FILL_SUPPLIED_OPTIONS = frozenset({"--lane", "--key", "--ticket", "--attempt"})
# The options the start entry requires that the fill does not fill per start. The static ones (the overlay path, the
# ledger path) belong in ``floor_fill.runner_command`` as fixed elements; ``--model``, ``--brief``, ``--run-id`` and
# ``--pool-snapshot`` are per-start values the fill has no source for today. The live binding step supplies them; this
# set changes only when the start entry or the fill changes shape, and the change is then read by its owner.
START_ENTRY_UNSUPPLIED = frozenset(
    {"--overlay", "--ledger", "--pool-snapshot", "--model", "--brief", "--run-id"}
)
START_ENTRY_PREFIX = [
    "onex",
    "run-node",
    "node_remote_lane_start_orchestrator",
    "--",
]

# floor_fill_state as omnibase_infra migration 113 creates it (OMN-23002).
MIGRATION_113_COLUMNS = frozenset(
    {
        "scope",
        "tenant_id",
        "state",
        "in_flight",
        "payload",
        "version",
        "pending_emissions",
        "publish_attempts",
        "created_at",
        "updated_at",
    }
)
MIGRATION_113_REQUIRED_WITHOUT_DEFAULT = frozenset(
    {"scope", "tenant_id", "state", "payload"}
)
MIGRATION_113_TABLE = "floor_fill_state"
MIGRATION_113_KEY = "scope"

# T11's mint result, node_floor_task_ticket_effect (OMN-22850): the states the fill reads.
MINT_STATES = ("ticket", "refused", "unavailable")
TICKET_SEAM_METHODS = frozenset({"payload_verdicts", "entry_ticket_verdicts", "mint"})


def _contract() -> dict[str, Any]:
    contract: dict[str, Any] = yaml.safe_load(
        (ORCHESTRATOR / "contract.yaml").read_text(encoding="utf-8")
    )
    return contract


def _start_parser_options() -> frozenset[str]:
    """Every option of the start entry's parser, read from its public usage text."""
    usage = " ".join(runtime_remote_lane_start.parser().format_usage().split())
    return frozenset(re.findall(r"--[a-z][a-z-]*", usage))


def _logged(log: Path, text: str) -> bool:
    return log.exists() and text in log.read_text(encoding="utf-8")


def _wait_for(check: Callable[[], bool], what: str, timeout: float = 15.0) -> None:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if check():
            return
        time.sleep(0.05)
    raise AssertionError(f"timed out waiting for {what}")


# ------------------------------------------------------------------ seam: process effect


class _RecordingPosix:
    """The real posix runner behind the orchestrator's seam; it remembers each pid so the test can end the groups."""

    synchronous_execution: ClassVar[bool] = True

    def __init__(self, output_dir: Path) -> None:
        self.inner = HandlerPosixProcess(str(output_dir))
        self.pids: list[int] = []

    def start_detached(self, spec: Any) -> Any:
        result = self.inner.start_detached(spec)
        if result.pid is not None:
            self.pids.append(result.pid)
        return result

    def run_bounded(self, spec: Any, timeout_seconds: float) -> Any:
        return self.inner.run_bounded(spec, timeout_seconds)


@contextlib.contextmanager
def _posix(tmp_path: Path) -> Iterator[_RecordingPosix]:
    """The recording runner for one test; every process group it started is ended when the test is done."""
    runner = _RecordingPosix(tmp_path / "out")
    yield runner
    for pid in runner.pids:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(pid, signal.SIGKILL)


def _real_config(tmp_path: Path, executable: str) -> dict[str, Any]:
    work = tmp_path / "work"
    work.mkdir(exist_ok=True)
    return overlay(
        max_fill_lanes_per_line=1,
        process_working_directory=str(work),
        runner_command=[
            executable,
            "-c",
            "import os, sys; "
            "print('seam', os.environ['FLOOR_FILL_KEY'], os.environ['FLOOR_FILL_TICKET'], "
            "os.environ['FLOOR_FILL_ATTEMPT'], *sys.argv[1:], flush=True); "
            "import time; time.sleep(30)",
            "{lane}",
            "{key}",
        ],
    )


def test_real_process_effect_starts_one_process_per_line(tmp_path: Path) -> None:
    """Four startable lines through the real posix runner: four processes, each told its key, ticket and attempt."""
    with _posix(tmp_path) as posix:
        h = harness(config=_real_config(tmp_path, sys.executable))
        answer = h.orchestrator(posix).run(EnumFloorFillCommand.TICK)
        assert [r.kind for r in answer.key_results] == ["started"] * 4
        assert len(posix.pids) == 4
        for result in answer.key_results:
            log = tmp_path / "out" / f"{result.lane}.log"
            _wait_for(
                functools.partial(_logged, log, "seam"),
                f"the output of {result.lane}",
            )
            text = log.read_text(encoding="utf-8")
            assert (
                f"seam {result.key} {result.ticket} 1 {result.lane} {result.key}"
                in text
            )


def test_real_process_effect_refusal_prints_the_missing_line(tmp_path: Path) -> None:
    """The failing control: an entry the runner cannot execute starts nothing and every line prints ``MISSING line-stopped:``."""
    with _posix(tmp_path) as posix:
        h = harness(config=_real_config(tmp_path, "no-such-entry-on-this-host"))
        answer = h.orchestrator(posix).run(EnumFloorFillCommand.TICK)
        assert [r.kind for r in answer.key_results] == ["failed"] * 4
        assert posix.pids == []
        missing = [x for x in answer.lines if x.startswith("MISSING line-stopped:")]
        assert len(missing) == 4


def test_every_runner_binds_the_spi_process_protocol(tmp_path: Path) -> None:
    """The scripted starter, the scripted runner and the posix runner are all a ``ProtocolLaneProcessRunner``."""
    assert isinstance(ScriptedStarter(), ProtocolLaneProcessRunner)
    assert isinstance(HandlerScriptedRunner(), ProtocolLaneProcessRunner)
    assert isinstance(HandlerPosixProcess(str(tmp_path)), ProtocolLaneProcessRunner)


# ------------------------------------------------------------------ seam: lane start entry


def test_fill_placeholders_are_options_of_the_start_entry() -> None:
    """Every placeholder the fill fills is an option of the start entry's own parser."""
    options = _start_parser_options()
    assert {f"--{name}" for name in FILL_PLACEHOLDERS} == FILL_SUPPLIED_OPTIONS
    assert options >= FILL_SUPPLIED_OPTIONS


def test_start_entry_requires_nothing_the_fill_cannot_supply_unpinned(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The argv the fill renders, read by the start entry: what it refuses for is the pinned set, and no more."""
    starter = ScriptedStarter()
    h = harness(
        config=overlay(
            max_fill_lanes_per_line=1,
            runner_command=[
                *START_ENTRY_PREFIX,
                *[
                    element
                    for name in FILL_PLACEHOLDERS
                    for element in (f"--{name}", "{" + name + "}")
                ],
            ],
        ),
        starter=starter,
    )
    h.orchestrator().run(EnumFloorFillCommand.TICK)
    assert len(starter.starts) == 4
    rendered = list(starter.starts[0].argv)
    assert rendered[: len(START_ENTRY_PREFIX)] == START_ENTRY_PREFIX
    parser = runtime_remote_lane_start.parser()
    with pytest.raises(SystemExit) as refused:
        parser.parse_args(rendered[len(START_ENTRY_PREFIX) :])
    assert refused.value.code == 2
    message = capsys.readouterr().err
    named = message.split("the following arguments are required:")[1].split("\n")[0]
    unsupplied = frozenset(part.strip() for part in named.split(","))
    assert unsupplied == START_ENTRY_UNSUPPLIED


def test_start_entry_parses_once_the_pinned_options_are_supplied() -> None:
    """The failing control of the pin: with the pinned options added, the same rendered argv is accepted whole."""
    starter = ScriptedStarter()
    h = harness(
        config=overlay(
            max_fill_lanes_per_line=1,
            runner_command=[
                *START_ENTRY_PREFIX,
                "--overlay",
                "/scripted/overlay.yaml",
                "--ledger",
                "/scripted/ledger.md",
                "--pool-snapshot",
                "/scripted/pool.json",
                "--model",
                "scripted-model",
                "--brief",
                "/scripted/brief.md",
                "--run-id",
                "scripted-run",
                *[
                    element
                    for name in FILL_PLACEHOLDERS
                    for element in (f"--{name}", "{" + name + "}")
                ],
            ],
        ),
        starter=starter,
    )
    h.orchestrator().run(EnumFloorFillCommand.TICK)
    start = starter.starts[0]
    args = runtime_remote_lane_start.parser().parse_args(
        list(start.argv[len(START_ENTRY_PREFIX) :])
    )
    assert (args.lane, args.key, args.ticket, args.attempt) == (
        start.lane,
        start.key,
        start.ticket,
        start.attempt,
    )


def test_start_node_example_names_a_fill_key() -> None:
    """The start node's example overlay sets ``floor_fill.runner_command``: a key the fill's config reads."""
    example = yaml.safe_load(
        (START_NODE / "overlay.example.yaml").read_text(encoding="utf-8")
    )
    assert set(example["floor_fill"]) <= set(ModelFloorFillConfig.model_fields)


# ------------------------------------------------------------------ seam: state table


class _Recorder:
    """A stand-in for psycopg2 that records each statement the store issues, as the text of its composition."""

    class Error(Exception):
        pass

    def __init__(self, rowcount: int = 1) -> None:
        self.statements: list[tuple[str, tuple[Any, ...]]] = []
        self.rowcount = rowcount

    def connect(self, *_: Any, **__: Any) -> Any:
        recorder = self

        class _Cursor:
            rowcount = recorder.rowcount

            def __enter__(self) -> _Cursor:
                return self

            def __exit__(self, *_: Any) -> None:
                return None

            def execute(self, statement: Any, params: tuple[Any, ...]) -> None:
                recorder.statements.append((repr(statement), params))

            def fetchone(self) -> None:
                return None

        class _Connection:
            autocommit = False

            def cursor(self) -> _Cursor:
                return _Cursor()

            def close(self) -> None:
                return None

        return _Connection()


def _store(monkeypatch: pytest.MonkeyPatch) -> tuple[Any, _Recorder]:
    recorder = _Recorder()
    monkeypatch.setattr(
        store_module,
        "psycopg2",
        SimpleNamespace(connect=recorder.connect, Error=_Recorder.Error),
    )
    settings = ModelFloorFillStoreSettings(
        dsn="postgresql://scripted",
        schema_name="public",
        table_name=MIGRATION_113_TABLE,
        connect_timeout_seconds=5,
    )
    return store_module.PostgresFloorStateStore(settings), recorder


def _columns(statement: str) -> set[str]:
    """The column list of the insert: the names between ``(`` and ``) VALUES`` in the composed statement."""
    match = re.search(r"\((scope[^)]*)\) VALUES", statement)
    assert match is not None, statement
    return {part.strip() for part in match.group(1).split(",")}


def test_contract_state_io_is_the_migration_table_and_key() -> None:
    state_io = _contract()["state_io"]
    assert (state_io["table"], state_io["key"]) == (
        MIGRATION_113_TABLE,
        MIGRATION_113_KEY,
    )


def test_insert_names_every_column_the_migration_leaves_without_a_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, recorder = _store(monkeypatch)
    assert store.compare_and_set("floor-fill-test", 0, 1, "{}") is True
    [(statement, params)] = recorder.statements
    written = _columns(statement)
    assert written <= MIGRATION_113_COLUMNS
    assert written >= MIGRATION_113_REQUIRED_WITHOUT_DEFAULT
    assert "ON CONFLICT (scope) DO NOTHING" in statement
    assert params == ("floor-fill-test", "{}", 1)


def test_update_is_conditional_on_the_version_the_writer_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, recorder = _store(monkeypatch)
    assert store.compare_and_set("floor-fill-test", 3, 4, "{}") is True
    [(statement, params)] = recorder.statements
    assert "SET payload = %s::jsonb, version = %s" in statement
    assert "WHERE scope = %s AND version = %s" in statement
    assert params == ("{}", 4, "floor-fill-test", 3)


def test_a_lost_race_is_a_refused_write_not_an_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(monkeypatch)[0]
    losing = _Recorder(rowcount=0)
    monkeypatch.setattr(
        store_module,
        "psycopg2",
        SimpleNamespace(connect=losing.connect, Error=_Recorder.Error),
    )
    assert store.compare_and_set("floor-fill-test", 3, 4, "{}") is False


def test_reads_name_the_migration_columns(monkeypatch: pytest.MonkeyPatch) -> None:
    store, recorder = _store(monkeypatch)
    assert store.load("floor-fill-test").version == 0
    [(statement, _)] = recorder.statements
    assert "SELECT payload::text, version" in statement
    assert "WHERE scope = %s" in statement


# ------------------------------------------------------------------ seam: tickets (T11)


def test_mint_result_states_are_the_ticket_effects() -> None:
    assert get_args(ModelMintResult.model_fields["state"].annotation) == MINT_STATES


def test_ticket_seam_methods_are_pinned() -> None:
    methods = {
        name
        for name in vars(ProtocolFloorTickets)
        if not name.startswith("_") and callable(vars(ProtocolFloorTickets)[name])
    }
    assert methods == TICKET_SEAM_METHODS


# ------------------------------------------------------------------ seam: overlay


def test_contract_keys_config_fields_and_example_are_one_list() -> None:
    keys = _contract()["overlay"]["keys"]
    assert set(keys) == set(ModelFloorFillConfig.model_fields)
    example = yaml.safe_load(
        (ORCHESTRATOR / "overlay.example.yaml").read_text(encoding="utf-8")
    )
    assert list(example["floor_fill"]) == keys
