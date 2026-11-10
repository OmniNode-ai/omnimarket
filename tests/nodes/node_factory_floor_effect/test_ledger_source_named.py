# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-23606: every ledger read of the floor and of the unresolved step names its row source, from the floor's contract.

On a host whose ledger record is the bus, the reader's default source is the bus read. The ledger node refuses an answer over its
917,516-byte bound (the 14-day window is 222 MB, three of the four unresolved reads 8.7 to 55.8 MB) after loading the whole table,
and the reader then falls back to the file. Measured 2026-11-10 on lab host h202, one unresolved read of 5,593 rows: 35.5 s by
default, 9.5 s with ``--source file``. The seam: the contract's ``ledger_source`` is what the reader is told, for the window read and
for each of the unresolved reads. The failing control is a reader that does what the default does when no source is named.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from omnimarket.nodes.node_factory_floor_effect.source_loader import (
    OWN_CONTRACT_PATH,
    SourceRefusedError,
    load_contract,
    read_ledger_rows,
    read_unresolved_rows,
)

pytestmark = pytest.mark.unit

SOURCE = load_contract(OWN_CONTRACT_PATH).runtime.ledger_source
ROW = "L1 2026-01-01T00:00:00Z | TERMINAL | lane=x | outcome=blocked | ticket=OMN-1 | text"


def _reader(tmp_path: Path) -> tuple[Path, Path]:
    plugin = tmp_path / "plugin"
    (plugin / "scripts").mkdir(parents=True)
    log = tmp_path / "argv.jsonl"
    body = (
        "import json, sys\n"
        f"open({str(log)!r}, 'a').write(json.dumps(sys.argv[1:]) + '\\n')\n"
        "args = sys.argv[1:]\n"
        "named = args[args.index('--source') + 1] if '--source' in args else None\n"
        "if named != 'file':\n"
        "    sys.stderr.write('bus read refused: answer over bound')\n"
        "    sys.exit(1)\n"
        f"print({ROW!r})\n"
    )
    (plugin / "scripts" / "onex_ledger.py").write_text(body, encoding="utf-8")
    return plugin, log


def _argvs(log: Path) -> list[list[str]]:
    return [json.loads(line) for line in log.read_text().splitlines()]


def test_the_contract_names_the_file_as_the_source() -> None:
    assert SOURCE == "file"


def test_golden_the_window_read_names_the_contracts_source(tmp_path: Path) -> None:
    plugin, log = _reader(tmp_path)
    rows = read_ledger_rows(
        window_days=14,
        cut_chars=1200,
        timeout_seconds=60,
        source=SOURCE,
        plugin_root=plugin,
        plugin_root_env="CLAUDE_PLUGIN_ROOT",
        ledger_rows_file=None,
    )
    assert len(rows) == 1
    (argv,) = _argvs(log)
    assert argv[argv.index("--source") + 1] == SOURCE


def test_golden_every_unresolved_read_names_the_contracts_source(
    tmp_path: Path,
) -> None:
    plugin, log = _reader(tmp_path)
    reads = (("TERMINAL", "outcome=blocked"), ("STATUS", "handed-off="))
    read_unresolved_rows(
        reads=reads,
        cut_chars=1200,
        timeout_seconds=60,
        source=SOURCE,
        plugin_root=plugin,
        plugin_root_env="CLAUDE_PLUGIN_ROOT",
        stand_in_file=None,
        kinds=frozenset({"TERMINAL"}),
        older_than=datetime(2027, 1, 1, tzinfo=UTC),
        max_workers=2,
    )
    argvs = _argvs(log)
    assert len(argvs) == len(reads)
    assert all(a[a.index("--source") + 1] == SOURCE for a in argvs)


def test_error_a_source_the_reader_refuses_is_a_named_refusal(tmp_path: Path) -> None:
    """The injected break: the bus source (what the default is on a bus-record host) is refused by the reader, and the loader
    raises naming the reader's own words, never an empty window."""
    plugin, _ = _reader(tmp_path)
    with pytest.raises(SourceRefusedError, match="answer over bound"):
        read_ledger_rows(
            window_days=14,
            cut_chars=1200,
            timeout_seconds=60,
            source="db",
            plugin_root=plugin,
            plugin_root_env="CLAUDE_PLUGIN_ROOT",
            ledger_rows_file=None,
        )
