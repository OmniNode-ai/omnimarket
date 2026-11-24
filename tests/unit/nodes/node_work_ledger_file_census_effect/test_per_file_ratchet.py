# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The per-file baseline: new code that opens the ledger file fails the census.

The per-repository file count alone passes a change that deletes one reader and adds another. The
per-file baseline lists every file that holds an executable reader or writer today with its line
count; a file that is not listed, or a listed file with more such lines than its row, is red. The
baseline only shrinks. Each red case has its green twin (the positive control).
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from omnimarket.handlers.emission_sink import InMemoryEmissionSink
from omnimarket.nodes.node_work_ledger_file_census_effect import runtime_file_census
from omnimarket.nodes.node_work_ledger_file_census_effect.adapters import (
    InMemoryFileBaselineStore,
    InMemoryRatchetStore,
    InMemoryRepositorySource,
    YamlFileBaselineStore,
    YamlRatchetStore,
)
from omnimarket.nodes.node_work_ledger_file_census_effect.handlers import (
    HandlerFileCensus,
)
from omnimarket.nodes.node_work_ledger_file_census_effect.models import (
    ModelCensusRequest,
)

from .support import CLEAN, NOW, PACKAGE, READER, WRITER, overlay

pytestmark = pytest.mark.unit

_RATCHET = {"alpha": 5, "beta": 5, "outside": 5}
_BASELINE = {"alpha": {"old_reader.py": 1}, "beta": {}, "outside": {}}


def _run(
    repos: dict[str, dict[str, str]],
    baseline: dict[str, dict[str, int]] | None,
    ratchet: dict[str, int] | None = None,
):
    handler = HandlerFileCensus(
        overlay(),
        InMemoryRepositorySource(repos),
        InMemoryRatchetStore(_RATCHET if ratchet is None else ratchet),
        InMemoryEmissionSink(),
        clock=lambda: NOW,
        backend="in_memory",
        baseline=None if baseline is None else InMemoryFileBaselineStore(baseline),
    )
    return handler.handle(ModelCensusRequest(correlation_id="c1"))


def _repos(**alpha: str) -> dict[str, dict[str, str]]:
    return {
        "alpha": {**CLEAN, "old_reader.py": READER, **alpha},
        "beta": {"x.py": "x = 1\n"},
        "outside": {},
    }


def test_baselined_reader_alone_is_green() -> None:
    result = _run(_repos(), _BASELINE)
    assert result.passed, result.failure_detail


def test_new_file_with_a_reader_is_red_though_the_count_is_under_the_ratchet() -> None:
    result = _run(_repos(**{"new_reader.py": READER}), _BASELINE)
    assert result.failure_code == "new-file-reader"
    assert "alpha:new_reader.py (1>0)" in result.failure_detail
    alpha = next(c for c in result.counts if c.repository == "alpha")
    assert alpha.files == 2 < (alpha.ratchet or 0)
    assert alpha.new_readers == ("new_reader.py (1>0)",)


def test_new_file_with_a_writer_is_red() -> None:
    result = _run(_repos(**{"new_writer.py": WRITER}), _BASELINE)
    assert result.failure_code == "new-file-reader"
    assert "new_writer.py" in result.failure_detail


def test_swapping_one_reader_for_another_is_red() -> None:
    swapped = {
        "alpha": {**CLEAN, "other_reader.py": READER},
        "beta": {"x.py": "x = 1\n"},
        "outside": {},
    }
    assert _run(swapped, _BASELINE).failure_code == "new-file-reader"


def test_a_second_reader_line_in_a_baselined_file_is_red() -> None:
    result = _run(_repos(**{"old_reader.py": READER + READER}), _BASELINE)
    assert result.failure_code == "new-file-reader"
    assert "old_reader.py (2>1)" in result.failure_detail


def test_a_reader_cleared_from_a_baselined_file_is_green_and_lowers_the_baseline() -> (
    None
):
    result = _run(_repos(**{"old_reader.py": "x = 1\n"}), _BASELINE)
    assert result.passed
    assert result.lowered_baseline["alpha"] == {}


def test_a_prose_mention_in_a_new_file_is_none() -> None:
    assert _run(_repos(**{"prose.py": CLEAN["src/a.py"]}), _BASELINE).passed


def test_repository_without_a_baseline_row_is_a_typed_failure() -> None:
    result = _run(_repos(), {"beta": {}, "outside": {}})
    assert result.failure_code == "baseline-missing-row"
    assert result.passed is False


def test_unreadable_baseline_is_a_typed_failure() -> None:
    handler = HandlerFileCensus(
        overlay(),
        InMemoryRepositorySource(_repos()),
        InMemoryRatchetStore(_RATCHET),
        InMemoryEmissionSink(),
        clock=lambda: NOW,
        backend="in_memory",
        baseline=InMemoryFileBaselineStore(None),
    )
    result = handler.handle(ModelCensusRequest(correlation_id="c2"))
    assert result.failure_code == "baseline-unreadable"


def test_without_a_baseline_only_the_count_is_held() -> None:
    assert _run(_repos(**{"new_reader.py": READER}), None).passed


def test_yaml_baseline_lower_never_raises_a_row_or_adds_a_file(tmp_path: Path) -> None:
    path = tmp_path / "ratchet.yaml"
    path.write_text(
        yaml.safe_dump(
            {"ratchet": {"alpha": 4}, "files": {"alpha": {"a.py": 2, "b.py": 1}}}
        )
    )
    store = YamlFileBaselineStore(path)
    store.write({"alpha": {"a.py": 5, "c.py": 1}}, "# header\n")
    assert store.read() == {"alpha": {"a.py": 2}}
    assert yaml.safe_load(path.read_text())["ratchet"] == {"alpha": 4}


def test_count_lowering_keeps_the_per_file_rows(tmp_path: Path) -> None:
    path = tmp_path / "ratchet.yaml"
    path.write_text(
        yaml.safe_dump({"ratchet": {"alpha": 4}, "files": {"alpha": {"a.py": 2}}})
    )
    YamlRatchetStore(path).write({"alpha": 3}, "# header\n")
    data = yaml.safe_load(path.read_text())
    assert data["ratchet"] == {"alpha": 3}
    assert data["files"] == {"alpha": {"a.py": 2}}


def test_shipped_ratchet_file_holds_the_per_file_table() -> None:
    data = yaml.safe_load((PACKAGE / "overlay.example.yaml").read_text())
    assert "per_file" not in data
    rows = YamlFileBaselineStore(PACKAGE / "ratchet.yaml").read()
    assert "omnimarket" in set(rows)
    assert set(rows) == {r["name"] for r in data["repositories"]}
    assert all(hits > 0 for table in rows.values() for hits in table.values())


def test_cli_exits_red_on_a_new_reader_and_green_without_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = tmp_path / "omnimarket"
    repo.mkdir()
    (repo / ".git").mkdir()
    # git grep needs a real repository
    import subprocess

    def git(*args: str) -> None:
        subprocess.run(
            ["git", "-C", str(repo), *args],
            check=True,
            capture_output=True,
            env={
                "PATH": "/usr/bin:/bin",
                "GIT_CONFIG_GLOBAL": "/dev/null",
                "GIT_CONFIG_SYSTEM": "/dev/null",
            },
        )

    (repo / ".git").rmdir()
    git("init", "-q")
    (repo / "old_reader.py").write_text(READER)
    ratchet = tmp_path / "ratchet.yaml"
    ratchet.write_text(
        yaml.safe_dump(
            {
                "ratchet": {"omnimarket": 5},
                "files": {"omnimarket": {"old_reader.py": 1}},
            }
        )
    )
    git("add", "-A")
    argv = [
        "--packaged-overlay",
        "--repos",
        "omnimarket",
        "--directory",
        f"omnimarket={repo}",
        "--ratchet-file",
        str(ratchet),
        "--per-file",
    ]
    env = {"ONEX_LEDGER_CENSUS_ROOT": str(tmp_path)}
    assert runtime_file_census.main(argv, environ=env) == 0
    (repo / "new_reader.py").write_text(READER)
    git("add", "-A")
    assert runtime_file_census.main(argv, environ=env) == 1
    out = capsys.readouterr().out
    assert "NEW-READER new_reader.py (1>0)" in out
    assert "failure=new-file-reader" in out
