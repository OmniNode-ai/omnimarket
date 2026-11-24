# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The daily census counts every registered repository, as the daily unit runs it (OMN-26318).

The registry list is spelled here, in this repository, so the suite runs on every runner. The packaged overlay and
the packaged ratchet file must hold exactly that list, one row and one per-file table each; a registry root built from
the committed per-file tables is counted by the daily tick (the unit's own entry) with a pass; a reader seeded into any
one repository turns the tick red with that repository named, and the ratchet file is left as it was.
"""

from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml
from omnibase_core.validators.no_unguarded_git_subprocess import (
    scrub_git_location_env,
)

from omnimarket.nodes.node_work_ledger_file_census_effect import (
    runtime_file_census_tick as tick,
)
from omnimarket.nodes.node_work_ledger_file_census_effect.handlers.classify import (
    is_runtime,
)
from omnimarket.nodes.node_work_ledger_file_census_effect.models import (
    ModelCensusOverlay,
)

from .support import PACKAGE

pytestmark = pytest.mark.unit

_REGISTRY = (
    "omnibase_compat",
    "omnibase_core",
    "omnibase_infra",
    "omnibase_spi",
    "omniclaude",
    "omnidash",
    "omniintelligence",
    "omnimarket",
    "omnimemory",
    "knowledge_base",
)
# spelled in two pieces so this file is no reader of the ledger file itself
_READER = "ONEX_LEDGER" + "_PATH=1\n"


def _rows() -> dict[str, int]:
    data = yaml.safe_load((PACKAGE / "ratchet.yaml").read_text(encoding="utf-8"))
    return {str(name): int(count) for name, count in data["ratchet"].items()}


def _tables() -> dict[str, dict[str, int]]:
    data = yaml.safe_load((PACKAGE / "ratchet.yaml").read_text(encoding="utf-8"))
    return {
        str(name): {str(path): int(lines) for path, lines in (table or {}).items()}
        for name, table in data["files"].items()
    }


def _overlay() -> ModelCensusOverlay:
    return ModelCensusOverlay.model_validate(
        yaml.safe_load((PACKAGE / "overlay.example.yaml").read_text(encoding="utf-8"))
    )


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        env=scrub_git_location_env(os.environ),
    )


def _write_tracked(repo: Path, path: str, lines: int) -> None:
    target = repo / path
    target.parent.mkdir(parents=True, exist_ok=True)
    # an extensionless file is code only by its shebang
    body = "#!/bin/sh\n" if not Path(path).suffix else ""
    target.write_text(body + _READER * lines, encoding="utf-8")
    _git(repo, "add", "-A")


@pytest.fixture(scope="module")
def committed_root(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A registry root whose repositories hold exactly the files the committed per-file tables name."""
    root = tmp_path_factory.mktemp("registry")
    tables = _tables()
    for name in _REGISTRY:
        repo = root / name
        repo.mkdir()
        _git(repo, "init", "-q")
        (repo / "README.md").write_text("a repository\n", encoding="utf-8")
        for path, lines in tables[name].items():
            _write_tracked(repo, path, lines)
        _git(repo, "add", "-A")
    return root


@pytest.fixture
def registry_root(committed_root: Path, tmp_path: Path) -> Path:
    root = tmp_path / "registry"
    shutil.copytree(committed_root, root)
    return root


class _AcceptingBus:
    def publish(self, **event: object) -> bool:
        return True


def _tick(root: Path, tmp_path: Path) -> tuple[int, list[str], dict[str, object]]:
    out = io.StringIO()
    state = tmp_path / "state"
    code = tick.main(
        ["--omni-home", str(root), "--state-dir", str(state)],
        environ={},
        stdout=out,
        publisher=_AcceptingBus(),
    )
    evidence = json.loads((state / tick.EVIDENCE_FILE).read_text(encoding="utf-8"))
    return code, out.getvalue().splitlines(), evidence


def test_all_repositories_ratchet_the_packaged_overlay_registers_every_registry_repository() -> (
    None
):
    overlay = _overlay()
    assert [r.name for r in overlay.repositories] == list(_REGISTRY)
    assert not any(r.own_line for r in overlay.repositories)


def test_all_repositories_ratchet_one_row_and_one_table_per_registry_repository() -> (
    None
):
    rows = _rows()
    tables = _tables()
    assert sorted(rows) == sorted(_REGISTRY)
    assert sorted(tables) == sorted(_REGISTRY)
    for name in _REGISTRY:
        assert rows[name] == len(tables[name]), name


def test_all_repositories_ratchet_runtime_code_is_counted_apart_in_every_repository() -> (
    None
):
    overlay = _overlay()
    tables = _tables()
    runtime = {
        name: sorted(p for p in tables[name] if is_runtime(p, overlay))
        for name in _REGISTRY
    }
    # a repository's tests, plan contracts and data files are held but are not runtime code
    for name in _REGISTRY:
        assert len(runtime[name]) <= len(tables[name]), name
    assert runtime["omnibase_core"] == []
    assert runtime["omnibase_infra"], "omnibase_infra holds runtime readers"
    assert runtime["omniclaude"], "omniclaude holds runtime readers"
    assert runtime["omnimarket"], "omnimarket holds runtime readers"


def test_daily_record_prints_one_line_per_registry_repository_and_the_runtime_total(
    registry_root: Path, tmp_path: Path
) -> None:
    code, lines, evidence = _tick(registry_root, tmp_path)
    assert code == 0, lines
    counted = [
        line.split()[1]
        for line in lines
        if line.startswith("CENSUS ") and " state=" in line
    ]
    assert counted == list(_REGISTRY)
    tables = _tables()
    overlay = _overlay()
    total = sum(len(tables[n]) for n in _REGISTRY)
    runtime = sum(1 for n in _REGISTRY for p in tables[n] if is_runtime(p, overlay))
    assert f"CENSUS total={total} runtime={runtime} passed=yes failure=none " in lines
    record = evidence["record"]
    assert isinstance(record, dict)
    assert [r["repository"] for r in record["repositories"]] == list(_REGISTRY)
    assert (record["total_files"], record["runtime_files"]) == (total, runtime)
    assert all(r["state"] == "ok" for r in record["repositories"])
    assert evidence["exit_code"] == 0


@pytest.mark.parametrize("seeded", _REGISTRY)
def test_daily_record_fails_and_names_the_repository_when_a_row_grows(
    seeded: str, registry_root: Path, tmp_path: Path
) -> None:
    _write_tracked(registry_root / seeded, "src/pkg/another_reader.py", 1)
    code, lines, evidence = _tick(registry_root, tmp_path)
    assert code == 1, lines
    record = evidence["record"]
    assert isinstance(record, dict)
    assert record["passed"] is False
    assert record["failure_code"] in ("new-file-reader", "ratchet-grew")
    row = next(r for r in record["repositories"] if r["repository"] == seeded)
    assert row["state"] in ("new-reader", "grew")
    assert row["files"] == row["ratchet"] + 1
    assert evidence["exit_code"] == 1


def test_daily_record_a_missing_clone_is_a_typed_failure_not_a_pass(
    registry_root: Path, tmp_path: Path
) -> None:
    shutil.rmtree(registry_root / "omnidash")
    code, _lines, evidence = _tick(registry_root, tmp_path)
    assert code == 69
    record = evidence["record"]
    assert isinstance(record, dict)
    assert record["passed"] is False
    assert record["failure_code"] == "repo-absent"
    row = next(r for r in record["repositories"] if r["repository"] == "omnidash")
    assert row["state"] == "absent"


@pytest.mark.parametrize("seeded", _REGISTRY)
def test_new_reader_any_repository_fails_the_daily_census_and_no_row_is_raised(
    seeded: str, registry_root: Path, tmp_path: Path
) -> None:
    before = (PACKAGE / "ratchet.yaml").read_bytes()
    _write_tracked(registry_root / seeded, "scripts/new_reader.sh", 1)
    code, lines, evidence = _tick(registry_root, tmp_path)
    assert code == 1, lines
    assert any(line == "  NEW-READER scripts/new_reader.sh (1>0)" for line in lines), (
        lines
    )
    record = evidence["record"]
    assert isinstance(record, dict)
    flagged = [r["repository"] for r in record["repositories"] if r["state"] != "ok"]
    assert flagged == [seeded]
    assert (PACKAGE / "ratchet.yaml").read_bytes() == before


def test_new_reader_any_repository_a_replaced_reader_under_the_row_is_still_red(
    registry_root: Path, tmp_path: Path
) -> None:
    held = _tables()["omnibase_core"]
    cleared = sorted(held)[0]
    (registry_root / "omnibase_core" / cleared).unlink()
    _write_tracked(registry_root / "omnibase_core", "src/pkg/replacement.py", 1)
    code, lines, _evidence = _tick(registry_root, tmp_path)
    assert code == 1, lines
    assert "  NEW-READER src/pkg/replacement.py (1>0)" in lines
