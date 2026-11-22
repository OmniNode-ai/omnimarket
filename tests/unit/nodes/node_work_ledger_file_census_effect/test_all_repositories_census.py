# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The census over every registered repository (OMN-26318).

One ratchet row and one per-file table per repository, runtime code counted apart from tests, docs, data
and plan contracts, one typed daily record of the counts, and a new reader in any counted repository red
while no row is ever raised to pass it. Each red case has its green twin (the positive control).
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest
import yaml
from omnibase_core.validators.no_unguarded_git_subprocess import (
    scrub_git_location_env,
)

from omnimarket.handlers.emission_sink import InMemoryEmissionSink
from omnimarket.nodes.node_work_ledger_file_census_effect import runtime_file_census
from omnimarket.nodes.node_work_ledger_file_census_effect.adapters import (
    InMemoryFileBaselineStore,
    InMemoryRatchetStore,
    InMemoryRepositorySource,
    YamlFileBaselineStore,
)
from omnimarket.nodes.node_work_ledger_file_census_effect.handlers import (
    HandlerFileCensus,
)
from omnimarket.nodes.node_work_ledger_file_census_effect.handlers.classify import (
    is_runtime,
)
from omnimarket.nodes.node_work_ledger_file_census_effect.models import (
    ModelCensusDailyRecord,
    ModelCensusOverlay,
    ModelCensusRequest,
)

from .support import NOW, PACKAGE, READER, WRITER

pytestmark = pytest.mark.unit

_REPOSITORIES = ("alpha", "beta", "gamma")


def _overlay() -> ModelCensusOverlay:
    data = yaml.safe_load((PACKAGE / "overlay.example.yaml").read_text())
    data["repositories"] = [{"name": n, "own_line": False} for n in _REPOSITORIES]
    return ModelCensusOverlay.model_validate(data)


def _repos(**extra: dict[str, str]) -> dict[str, dict[str, str]]:
    base = {
        "alpha": {
            "src/pkg/reader.py": READER,
            "src/pkg/nodes/n/contract.yaml": READER,
            "tests/test_reader.py": READER,
            "contracts/OMN-1.yaml": READER,
        },
        "beta": {
            "src/pkg/writer.py": WRITER,
            "tools/data/rows.json": READER,
        },
        "gamma": {"src/pkg/clean.py": "x = 1\n"},
    }
    base.update(extra)
    return base


_RATCHET = {"alpha": 4, "beta": 2, "gamma": 0}
_BASELINE = {
    "alpha": {
        "src/pkg/reader.py": 1,
        "src/pkg/nodes/n/contract.yaml": 1,
        "tests/test_reader.py": 1,
        "contracts/OMN-1.yaml": 1,
    },
    "beta": {"src/pkg/writer.py": 1, "tools/data/rows.json": 1},
    "gamma": {},
}


def _run(
    repos: dict[str, dict[str, str]],
    ratchet: dict[str, int] | None = None,
    baseline: dict[str, dict[str, int]] | None = None,
):
    return HandlerFileCensus(
        _overlay(),
        InMemoryRepositorySource(repos),
        InMemoryRatchetStore(_RATCHET if ratchet is None else ratchet),
        InMemoryEmissionSink(),
        clock=lambda: NOW,
        backend="in_memory",
        baseline=InMemoryFileBaselineStore(_BASELINE if baseline is None else baseline),
    ).handle(ModelCensusRequest(correlation_id="c1"))


@pytest.mark.parametrize(
    ("path", "runtime"),
    [
        ("src/omnimarket/handlers/x.py", True),
        ("src/omnimarket/nodes/node_a/contract.yaml", True),
        ("plugins/onex/hooks/lib/x.py", True),
        ("scripts/ci/x.py", True),
        ("src/omnimarket/nodes/node_a/tests/test_a.py", False),
        ("tests/test_a.py", False),
        ("docs/guide.py", False),
        ("contracts/OMN-1.yaml", False),
        ("src/pkg/contracts/runtime.py", True),
        ("src/omnimarket/nodes/node_a/data/rows.yaml", False),
        ("src/omnimarket/nodes/node_a/fixtures/day.json", False),
        ("benchmarks/r2/bundle.json", False),
        ("README.md", False),
    ],
)
def test_all_repositories_ratchet_runtime_code_is_counted_apart(
    path: str, runtime: bool
) -> None:
    assert is_runtime(path, _overlay()) is runtime


def test_all_repositories_ratchet_one_row_and_one_table_per_repository() -> None:
    result = _run(_repos())
    assert result.passed, result.failure_detail
    by_name = {c.repository: c for c in result.counts}
    assert set(by_name) == set(_REPOSITORIES)
    assert {n: c.files for n, c in by_name.items()} == _RATCHET
    # alpha: the source reader and the runtime contract are runtime code; the test and the plan contract are not
    assert by_name["alpha"].runtime_files == 2
    # beta: the writer is runtime code; the json under a data directory is not
    assert by_name["beta"].runtime_files == 1
    assert by_name["gamma"].runtime_files == 0
    assert result.total_files == 6
    assert result.runtime_files == 3


def test_all_repositories_ratchet_a_repository_without_a_row_is_a_typed_failure() -> (
    None
):
    result = _run(_repos(), ratchet={"alpha": 4, "beta": 2})
    assert result.failure_code == "ratchet-missing-row"
    assert result.failure_detail == "gamma"


def test_all_repositories_ratchet_a_repository_without_a_table_is_a_typed_failure() -> (
    None
):
    result = _run(
        _repos(), baseline={"alpha": _BASELINE["alpha"], "beta": _BASELINE["beta"]}
    )
    assert result.failure_code == "baseline-missing-row"
    assert result.failure_detail == "gamma"


def test_all_repositories_ratchet_a_run_of_one_repository_keeps_the_others_rows(
    tmp_path: Path,
) -> None:
    path = tmp_path / "ratchet.yaml"
    path.write_text(yaml.safe_dump({"ratchet": _RATCHET, "files": _BASELINE}))
    store = YamlFileBaselineStore(path)
    store.write({"alpha": {"src/pkg/reader.py": 1}}, "")
    written = yaml.safe_load(path.read_text())["files"]
    assert written["alpha"] == {"src/pkg/reader.py": 1}
    assert written["beta"] == _BASELINE["beta"]
    assert written["gamma"] == {}


def test_daily_record_holds_every_repository_and_the_runtime_count() -> None:
    result = _run(_repos())
    record = ModelCensusDailyRecord.from_result(result, "2026-11-22")
    assert record.passed
    assert record.recorded_on == "2026-11-22"
    assert (record.total_files, record.runtime_files) == (6, 3)
    assert {r.repository: (r.files, r.runtime_files) for r in record.repositories} == {
        "alpha": (4, 2),
        "beta": (2, 1),
        "gamma": (0, 0),
    }
    assert (
        ModelCensusDailyRecord.model_validate_json(record.model_dump_json()) == record
    )


def test_daily_record_names_the_failure_when_a_row_grows() -> None:
    result = _run(_repos(), ratchet={"alpha": 3, "beta": 2, "gamma": 0})
    record = ModelCensusDailyRecord.from_result(result, "2026-11-22")
    assert not record.passed
    assert record.failure_code == "ratchet-grew"
    alpha = next(r for r in record.repositories if r.repository == "alpha")
    assert (alpha.files, alpha.ratchet, alpha.state) == (4, 3, "grew")


def _git_repo(root: Path, name: str, files: dict[str, str]) -> None:
    repo = root / name
    repo.mkdir()
    for path, text in files.items():
        (repo / path).parent.mkdir(parents=True, exist_ok=True)
        (repo / path).write_text(text)
    for args in (("init", "-q"), ("add", "-A")):
        subprocess.run(
            ["git", "-C", str(repo), *args],
            check=True,
            capture_output=True,
            env=scrub_git_location_env(os.environ),
        )


def _cli_env(tmp_path: Path) -> tuple[list[str], dict[str, str]]:
    data = yaml.safe_load((PACKAGE / "overlay.example.yaml").read_text())
    data["repositories"] = [{"name": n, "own_line": False} for n in _REPOSITORIES]
    overlay_file = tmp_path / "overlay.yaml"
    overlay_file.write_text(yaml.safe_dump(data))
    ratchet = tmp_path / "ratchet.yaml"
    ratchet.write_text(yaml.safe_dump({"ratchet": _RATCHET, "files": _BASELINE}))
    argv = ["--ratchet-file", str(ratchet), "--per-file", "--record"]
    env = {
        "ONEX_LEDGER_CENSUS_ROOT": str(tmp_path / "root"),
        "ONEX_LEDGER_CENSUS_OVERLAY": str(overlay_file),
    }
    return argv, env


def test_daily_record_cli_prints_one_line_per_repository_and_one_record(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = tmp_path / "root"
    root.mkdir()
    for name, files in _repos().items():
        _git_repo(root, name, files)
    argv, env = _cli_env(tmp_path)
    assert runtime_file_census.main(argv, environ=env) == 0
    out = capsys.readouterr().out.splitlines()
    assert [
        line.split()[1]
        for line in out
        if line.startswith("CENSUS ") and " state=" in line
    ] == list(_REPOSITORIES)
    assert "CENSUS total=6 runtime=3 passed=yes failure=none " in out
    records = [line for line in out if line.startswith("CENSUS-RECORD ")]
    assert len(records) == 1
    record = json.loads(records[0].removeprefix("CENSUS-RECORD "))
    assert (record["total_files"], record["runtime_files"]) == (6, 3)


@pytest.mark.parametrize("seeded", _REPOSITORIES)
def test_new_reader_any_repository_fails_the_census(seeded: str) -> None:
    clean = _run(_repos())
    assert clean.passed
    result = _run(_repos(**{seeded: {**_repos()[seeded], "src/pkg/new.py": READER}}))
    assert not result.passed
    assert result.failure_code in ("new-file-reader", "ratchet-grew")
    assert f"{seeded}" in result.failure_detail
    flagged = next(c for c in result.counts if c.repository == seeded)
    assert flagged.state in ("new-reader", "grew")
    # the run lowers nothing and raises nothing: a failed run returns no ratchet to write
    assert result.lowered_ratchet == {} or all(
        v <= _RATCHET[k] for k, v in result.lowered_ratchet.items()
    )


@pytest.mark.parametrize("seeded", _REPOSITORIES)
def test_new_reader_any_repository_is_red_though_the_count_is_under_its_row(
    seeded: str,
) -> None:
    # a reader that replaces one the table holds keeps the count at its row; a new file is still red
    roomy = dict.fromkeys(_REPOSITORIES, 10)
    result = _run(
        _repos(**{seeded: {**_repos()[seeded], "src/pkg/new.py": READER}}),
        ratchet=roomy,
    )
    assert result.failure_code == "new-file-reader"
    assert f"{seeded}:src/pkg/new.py (1>0)" in result.failure_detail


def test_new_reader_any_repository_a_cleared_reader_lowers_a_row_and_never_raises_one() -> (
    None
):
    repos = _repos()
    del repos["beta"]["src/pkg/writer.py"]
    result = _run(repos)
    assert result.passed
    assert result.lowered_ratchet["beta"] == 1
    assert all(v <= _RATCHET[k] for k, v in result.lowered_ratchet.items())
    assert "src/pkg/writer.py" not in result.lowered_baseline["beta"]
