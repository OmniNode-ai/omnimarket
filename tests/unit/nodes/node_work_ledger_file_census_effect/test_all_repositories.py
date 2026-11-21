# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The census over every registry repository, its daily typed record and the new-reader check on any of them.

Each red case has its green twin. Git-backed cases build real checkouts under ``tmp_path`` and drive the node's own CLI.
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
    read_registry_clone_aliases,
    read_registry_repositories,
    resolve_directories,
)
from omnimarket.nodes.node_work_ledger_file_census_effect.handlers import (
    HandlerFileCensus,
)
from omnimarket.nodes.node_work_ledger_file_census_effect.handlers.surface import (
    classify_surface,
)
from omnimarket.nodes.node_work_ledger_file_census_effect.models import (
    EnumCensusSurface,
    ModelCensusDailyRecord,
    ModelCensusOverlay,
    ModelCensusRepository,
    ModelCensusRequest,
)

from .support import CLEAN, NOW, PACKAGE, READER, WRITER, overlay

pytestmark = pytest.mark.unit

_REGISTRY = (
    "repositories:\n"
    "  - {name: alpha, repo: Org/alpha}\n"
    "  - {name: beta, repo: Org/beta}\n"
    "  - {name: gamma_repo, repo: Org/gamma-repo}\n"
)
_NAMES = ("alpha", "beta", "gamma_repo")


def _git_checkout(path: Path, files: dict[str, str]) -> None:
    path.mkdir(parents=True)
    for name, text in files.items():
        target = path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
    subprocess.run(
        ["git", "init", "-q", str(path)],
        check=True,
        env=scrub_git_location_env(os.environ),
    )
    subprocess.run(
        ["git", "-C", str(path), "add", "-A"],
        check=True,
        env=scrub_git_location_env(os.environ),
    )


def _world(tmp_path: Path, *, absent: tuple[str, ...] = ()) -> tuple[Path, Path]:
    """A registry root with a checkout per registered repository and the registry's clone list.

    ``gamma_repo`` is cloned under its repository's spelling, ``gamma-repo``.
    """
    root = tmp_path / "root"
    root.mkdir()
    registry = tmp_path / "repos.yaml"
    registry.write_text(_REGISTRY, encoding="utf-8")
    for name in _NAMES:
        if name in absent:
            continue
        directory = "gamma-repo" if name == "gamma_repo" else name
        _git_checkout(
            root / directory,
            {
                "src/pkg/old_reader.py": READER,
                "src/pkg/clean.py": CLEAN["src/a.py"],
                "docs/ruling.md": CLEAN["docs/ruling.md"],
            },
        )
    return root, registry


def _cli(
    root: Path,
    registry: Path,
    ratchet: Path,
    *extra: str,
) -> tuple[int, str]:
    lines: list[str] = []

    class _Out:
        def write(self, text: str) -> int:
            lines.append(text)
            return len(text)

    import sys

    original = sys.stdout
    sys.stdout = _Out()  # type: ignore[assignment]
    try:
        code = runtime_file_census.main(
            [
                "--packaged-overlay",
                "--registry",
                str(registry),
                "--ratchet-file",
                str(ratchet),
                "--per-file",
                *extra,
            ],
            environ={"ONEX_LEDGER_CENSUS_ROOT": str(root)},
        )
    finally:
        sys.stdout = original
    return code, "".join(lines)


def _empty_ratchet(tmp_path: Path) -> Path:
    path = tmp_path / "ratchet.yaml"
    path.write_text("ratchet: {}\nfiles: {}\n", encoding="utf-8")
    return path


# ---- every registry repository is counted -------------------------------------------------------------------------


def test_all_repositories_ratchet_registry_names_every_repository(
    tmp_path: Path,
) -> None:
    registry = tmp_path / "repos.yaml"
    registry.write_text(_REGISTRY, encoding="utf-8")
    repositories = read_registry_repositories(registry)
    assert tuple(r.name for r in repositories) == _NAMES
    assert not any(r.own_line for r in repositories)
    assert read_registry_clone_aliases(registry) == {"gamma_repo": "gamma-repo"}


@pytest.mark.parametrize(
    "text", ["", "repositories: []\n", "repositories: [{repo: x}]\n", "a: [\n"]
)
def test_all_repositories_ratchet_unreadable_registry_is_never_an_empty_census(
    tmp_path: Path, text: str
) -> None:
    registry = tmp_path / "repos.yaml"
    registry.write_text(text, encoding="utf-8")
    with pytest.raises((ValueError, yaml.YAMLError)):
        read_registry_repositories(registry)
    code, _ = _cli(tmp_path, registry, _empty_ratchet(tmp_path))
    assert code == 69


def test_all_repositories_ratchet_seed_writes_one_row_and_one_table_per_repository(
    tmp_path: Path,
) -> None:
    root, registry = _world(tmp_path)
    ratchet = _empty_ratchet(tmp_path)
    code, out = _cli(root, registry, ratchet)
    assert code == 69, "a repository with no row is a typed failure before it is seeded"
    assert "state=unratcheted" in out

    code, out = _cli(root, registry, ratchet, "--seed")
    assert code == 0, out
    data = yaml.safe_load(ratchet.read_text(encoding="utf-8"))
    assert data["ratchet"] == dict.fromkeys(_NAMES, 1)
    assert data["files"] == {name: {"src/pkg/old_reader.py": 1} for name in _NAMES}
    for name in _NAMES:
        assert f"CENSUS {name} counted state=ok files=1" in out


def test_all_repositories_ratchet_absent_clone_fails_closed_and_is_never_seeded(
    tmp_path: Path,
) -> None:
    root, registry = _world(tmp_path, absent=("beta",))
    ratchet = _empty_ratchet(tmp_path)
    code, out = _cli(root, registry, ratchet, "--seed")
    assert code == 69
    assert "CENSUS beta counted state=absent" in out
    assert "failure=repo-absent beta" in out
    data = yaml.safe_load(ratchet.read_text(encoding="utf-8"))
    assert set(data["ratchet"]) == {"alpha", "gamma_repo"}, "beta is not seeded"
    assert "beta" not in data["files"]


def test_all_repositories_ratchet_checkout_with_no_tracked_files_is_absent_not_zero(
    tmp_path: Path,
) -> None:
    root, registry = _world(tmp_path, absent=("beta",))
    (root / "beta").mkdir()
    subprocess.run(
        ["git", "init", "-q", str(root / "beta")],
        check=True,
        env=scrub_git_location_env(os.environ),
    )
    code, out = _cli(root, registry, _empty_ratchet(tmp_path), "--seed")
    assert code == 69
    assert "CENSUS beta counted state=absent" in out


def test_all_repositories_ratchet_seed_never_changes_a_row_already_held(
    tmp_path: Path,
) -> None:
    root, registry = _world(tmp_path)
    ratchet = tmp_path / "ratchet.yaml"
    ratchet.write_text(
        "ratchet: {alpha: 9}\nfiles:\n  alpha: {src/pkg/old_reader.py: 5, src/pkg/gone.py: 4}\n",
        encoding="utf-8",
    )
    code, _ = _cli(root, registry, ratchet, "--seed")
    assert code == 0
    data = yaml.safe_load(ratchet.read_text(encoding="utf-8"))
    assert data["ratchet"]["alpha"] == 9, (
        "a held row is neither raised nor lowered by --seed"
    )
    assert data["files"]["alpha"] == {"src/pkg/old_reader.py": 5, "src/pkg/gone.py": 4}
    assert data["ratchet"]["beta"] == 1


def test_all_repositories_ratchet_directory_alias_only_when_only_the_alias_is_a_checkout(
    tmp_path: Path,
) -> None:
    root, _ = _world(tmp_path)
    aliases = {"gamma_repo": "gamma-repo"}
    assert resolve_directories(root, aliases, {}) == {"gamma_repo": "gamma-repo"}
    assert resolve_directories(root, aliases, {"gamma_repo": "elsewhere"}) == {
        "gamma_repo": "elsewhere"
    }
    assert resolve_directories(tmp_path / "nowhere", aliases, {}) == {}


def test_all_repositories_ratchet_committed_file_holds_a_row_and_a_table_per_repository() -> (
    None
):
    data = yaml.safe_load((PACKAGE / "ratchet.yaml").read_text(encoding="utf-8"))
    rows, tables = data["ratchet"], data["files"]
    assert set(rows) == set(tables)
    assert rows["omnimarket"] <= 186, "a committed row is never raised"
    assert len(rows) > 1, "the census is no longer one repository"
    for repository, files in rows.items():
        assert len(tables[repository]) <= files, repository


# ---- the daily typed record ---------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("path", "surface"),
    [
        ("src/omnimarket/handlers/a.py", EnumCensusSurface.RUNTIME),
        ("src/omnimarket/nodes/node_x/contract.yaml", EnumCensusSurface.RUNTIME),
        ("src/omnimarket/nodes/node_x/data/patterns.yaml", EnumCensusSurface.RUNTIME),
        ("src/omnimarket/nodes/node_x/tests/test_a.py", EnumCensusSurface.TESTS),
        ("tests/unit/test_a.py", EnumCensusSurface.TESTS),
        ("tests/fixtures/ledger.txt", EnumCensusSurface.TESTS),
        ("docs/plan.md", EnumCensusSurface.DOCS),
        ("README.md", EnumCensusSurface.DOCS),
        ("contracts/OMN-1.yaml", EnumCensusSurface.PLAN_CONTRACT),
        ("benchmarks/bundles/R2-04.json", EnumCensusSurface.DATA),
        (".github/workflows/nightly.yml", EnumCensusSurface.DATA),
        ("scripts/ci/run.sh", EnumCensusSurface.DATA),
    ],
)
def test_daily_record_surface_of_a_counted_file(
    path: str, surface: EnumCensusSurface
) -> None:
    assert classify_surface(path) is surface


def _census(
    repos: dict[str, dict[str, str]],
    ratchet: dict[str, int],
    baseline: dict[str, dict[str, int]],
) -> tuple[HandlerFileCensus, InMemoryEmissionSink, ModelCensusOverlay]:
    sink = InMemoryEmissionSink()
    base = overlay()
    names = tuple(repos)
    base = base.model_copy(
        update={
            "repositories": tuple(
                ModelCensusRepository(name=n, own_line=False) for n in names
            )
        }
    )
    return (
        HandlerFileCensus(
            base,
            InMemoryRepositorySource(repos),
            InMemoryRatchetStore(ratchet),
            sink,
            clock=lambda: NOW,
            backend="in_memory",
            baseline=InMemoryFileBaselineStore(baseline),
        ),
        sink,
        base,
    )


_MIXED = {
    "src/a.py": READER,
    "src/nodes/n/contract.yaml": READER,
    "tests/test_a.py": READER,
    "docs/x.md": CLEAN["docs/ruling.md"],
    "contracts/OMN-1.yaml": READER,
    "scripts/run.sh": READER,
}
_MIXED_BASELINE = {
    "src/a.py": 1,
    "src/nodes/n/contract.yaml": 1,
    "tests/test_a.py": 1,
    "contracts/OMN-1.yaml": 1,
    "scripts/run.sh": 1,
}


def test_daily_record_is_one_typed_record_with_runtime_code_apart() -> None:
    census, sink, _ = _census(
        {"alpha": _MIXED, "beta": {"x.py": "x = 1\n"}},
        {"alpha": 5, "beta": 0},
        {"alpha": _MIXED_BASELINE, "beta": {}},
    )
    result = census.handle(ModelCensusRequest(correlation_id="d1"))
    assert result.passed, result.failure_detail
    record = result.daily_record
    assert isinstance(record, ModelCensusDailyRecord)
    alpha = next(r for r in record.repositories if r.repository == "alpha")
    assert alpha.files == 5
    assert alpha.surfaces.runtime == 2
    assert alpha.surfaces.tests == 1
    assert alpha.surfaces.plan_contract == 1
    assert alpha.surfaces.data == 1
    assert alpha.surfaces.total == alpha.files
    beta = next(r for r in record.repositories if r.repository == "beta")
    assert beta.files == 0
    assert beta.surfaces.total == 0
    assert record.total_files == 5
    assert record.runtime_files == 2
    assert [r.repository for r in record.repositories] == ["alpha", "beta"]
    success = [e for e in sink.events if e.phase == "success"]
    assert len(success) == 1, "one record per run, on the run's one chain"
    assert success[0].counters == record.counters()
    assert success[0].counters["runtime_files"] == 2
    assert success[0].counters["files:alpha"] == 5
    assert success[0].counters["runtime:alpha"] == 2
    assert (
        ModelCensusDailyRecord.model_validate_json(record.model_dump_json()) == record
    )


def test_daily_record_still_publishes_when_a_row_grew() -> None:
    census, sink, _ = _census(
        {"alpha": _MIXED},
        {"alpha": 4},
        {"alpha": {**_MIXED_BASELINE, "src/a.py": 2}},
    )
    result = census.handle(ModelCensusRequest(correlation_id="d2"))
    assert result.passed is False
    assert result.failure_code == "ratchet-grew"
    assert result.daily_record is not None
    assert result.daily_record.passed is False
    assert result.daily_record.failure_code == "ratchet-grew"
    assert result.daily_record.runtime_files == 2
    assert [e.phase for e in sink.events] == ["start", "failure"]


def test_daily_record_cli_writes_the_json_and_prints_the_census_lines(
    tmp_path: Path,
) -> None:
    root, registry = _world(tmp_path)
    ratchet = _empty_ratchet(tmp_path)
    out_file = tmp_path / "record.json"
    code, out = _cli(root, registry, ratchet, "--seed", "--daily-record", str(out_file))
    assert code == 0, out
    record = ModelCensusDailyRecord.model_validate_json(out_file.read_text("utf-8"))
    assert [r.repository for r in record.repositories] == list(_NAMES)
    assert record.runtime_files == 3
    assert record.passed is True
    census_lines = [
        line
        for line in out.splitlines()
        if line.startswith("CENSUS ") and " counted " in line
    ]
    assert len(census_lines) == len(_NAMES), "one CENSUS line per registry repository"
    assert "CENSUS runtime-code files=3" in out
    assert sum(1 for line in out.splitlines() if line.startswith("CENSUS-RECORD ")) == 1
    assert (
        json.loads(out.split("CENSUS-RECORD ", 1)[1].splitlines()[0])["runtime_files"]
        == 3
    )


def test_daily_record_cli_fails_when_a_row_grew(tmp_path: Path) -> None:
    root, registry = _world(tmp_path)
    ratchet = _empty_ratchet(tmp_path)
    assert _cli(root, registry, ratchet, "--seed")[0] == 0
    data = yaml.safe_load(ratchet.read_text(encoding="utf-8"))
    data["ratchet"]["beta"] = 0
    ratchet.write_text(yaml.safe_dump(data), encoding="utf-8")
    out_file = tmp_path / "record.json"
    code, out = _cli(root, registry, ratchet, "--daily-record", str(out_file))
    assert code == 1
    assert "failure=ratchet-grew beta=1>0" in out
    record = ModelCensusDailyRecord.model_validate_json(out_file.read_text("utf-8"))
    assert record.passed is False
    assert record.failure_code == "ratchet-grew"


# ---- a new reader in any counted repository -----------------------------------------------------------------------


@pytest.mark.parametrize("seeded", ["alpha", "beta", "gamma"])
@pytest.mark.parametrize(("kind", "text"), [("reader", READER), ("writer", WRITER)])
def test_new_reader_any_repository_fails_the_census(
    seeded: str, kind: str, text: str
) -> None:
    repos = {
        name: {"src/old.py": READER, "src/clean.py": CLEAN["src/a.py"]}
        for name in ("alpha", "beta", "gamma")
    }
    ratchet = dict.fromkeys(repos, 5)
    baseline = {name: {"src/old.py": 1} for name in repos}
    census, _, _ = _census(repos, ratchet, baseline)
    assert census.handle(ModelCensusRequest(correlation_id="g")).passed, "green twin"

    repos[seeded][f"src/new_{kind}.py"] = text
    census, _, _ = _census(repos, ratchet, baseline)
    result = census.handle(ModelCensusRequest(correlation_id="r"))
    assert result.passed is False
    assert result.failure_code == "new-file-reader"
    assert f"{seeded}:src/new_{kind}.py (1>0)" in result.failure_detail
    assert result.lowered_ratchet.get(seeded, 5) <= 5, (
        "a row is never raised to pass it"
    )
    assert all(
        count.state == "lowerable"
        for count in result.counts
        if count.repository != seeded
    )


def test_new_reader_any_repository_cli_seeded_checkout_stays_red_and_the_file_unchanged(
    tmp_path: Path,
) -> None:
    root, registry = _world(tmp_path)
    ratchet = _empty_ratchet(tmp_path)
    assert _cli(root, registry, ratchet, "--seed")[0] == 0
    before = ratchet.read_bytes()
    for directory in ("alpha", "beta", "gamma-repo"):
        planted = root / directory / "src/pkg/planted.py"
        planted.write_text(READER, encoding="utf-8")
        subprocess.run(
            ["git", "-C", str(root / directory), "add", "-A"],
            check=True,
            env=scrub_git_location_env(os.environ),
        )
        code, out = _cli(root, registry, ratchet, "--seed", "--lower")
        assert code == 1, (directory, out)
        assert "NEW-READER src/pkg/planted.py (1>0)" in out
        assert ratchet.read_bytes() == before, (
            "--seed and --lower never add the new reader's row"
        )
        subprocess.run(
            ["git", "-C", str(root / directory), "rm", "-qf", "src/pkg/planted.py"],
            check=True,
            env=scrub_git_location_env(os.environ),
        )
        assert _cli(root, registry, ratchet)[0] == 0, (
            "green twin once the reader is gone"
        )
