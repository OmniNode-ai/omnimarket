# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-22917, plan 4.3 reading 2: the census of executable ledger-file readers and writers, held by a ratchet.

A comment, a docstring, a document and a ruling's line citation count as none. One seeded reader
added to a fixture repository turns the ratchet red, and the same fixture without it is green (the
positive control).
"""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

import pytest
import yaml
from omnibase_core.validators.no_unguarded_git_subprocess import (
    scrub_git_location_env,
)

from omnimarket.handlers.emission_sink import InMemoryEmissionSink
from omnimarket.nodes.node_work_ledger_file_census_effect.adapters import (
    GitRepositorySource,
    YamlRatchetStore,
)
from omnimarket.nodes.node_work_ledger_file_census_effect.handlers import (
    HandlerFileCensus,
)
from omnimarket.nodes.node_work_ledger_file_census_effect.handlers.classify import (
    executable_hits,
)
from omnimarket.nodes.node_work_ledger_file_census_effect.models import (
    EnumCensusKind,
    ModelCensusOverlay,
    ModelCensusRequest,
)
from omnimarket.nodes.node_work_ledger_file_census_effect.runtime_file_census import (
    main,
)
from tests.unit.nodes.node_work_ledger_file_census_effect.support import (
    CLEAN,
    NOW,
    PACKAGE,
    READER,
    WRITER,
    handler,
    overlay,
    repositories,
)

pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[3]
RATCHET = ROOT / ".onex_ratchets/ledger_file_census_baseline.yaml"


def hits(path: str, text: str) -> list[tuple[int, EnumCensusKind]]:
    return [(h.line, h.kind) for h in executable_hits(path, text, overlay())]


@pytest.mark.parametrize(
    ("path", "text"),
    [
        ("a.py", '"""docstring ROLLING_WORK_LEDGER and ONEX_LEDGER_PATH"""\nx = 1\n'),
        ("a.py", "x = 1  # ONEX_LEDGER_PATH comment\n"),
        ("a.py", "# ROLLING_WORK_LEDGER.md\n"),
        ("a.py", 'x = "see ROLLING_WORK_LEDGER.md:3952"\n'),
        ("run.sh", "# cat $ONEX_LEDGER_PATH\n"),
        ("run.sh", "echo ok # ROLLING_WORK_LEDGER\n"),
        ("c.yaml", "key: v  # ROLLING_WORK_LEDGER\n"),
        ("n.md", "cat $ONEX_LEDGER_PATH > ROLLING_WORK_LEDGER.md\n"),
        ("a.py", "ONEX_LEDGER_PATH_OTHER = 1\n"),
        ("pic.png", "ROLLING_WORK_LEDGER\n"),
    ],
)
def test_comment_docstring_document_and_citation_count_as_none(
    path: str, text: str
) -> None:
    assert hits(path, text) == []


@pytest.mark.parametrize(
    ("path", "text", "kind"),
    [
        ("a.py", READER, EnumCensusKind.READER),
        ("a.py", WRITER, EnumCensusKind.WRITER),
        ("run.sh", 'tail -n 5 "$ONEX_LEDGER_PATH"\n', EnumCensusKind.READER),
        ("run.sh", 'echo row >> "$ONEX_LEDGER_PATH"\n', EnumCensusKind.WRITER),
        ("u.plist", "<string>ONEX_LEDGER_PATH</string>\n", EnumCensusKind.READER),
        ("tool", "#!/bin/sh\ncat ROLLING_WORK_LEDGER.md\n", EnumCensusKind.READER),
    ],
)
def test_executable_mentions_count(path: str, text: str, kind: EnumCensusKind) -> None:
    found = hits(path, text)
    assert len(found) == 1
    assert found[0][1] is kind


def test_census_counts_clean_fixture_as_zero_and_ratchet_holds_it() -> None:
    census, _ = handler(repositories(), {"alpha": 0, "beta": 0, "outside": 0})
    result = census.handle(ModelCensusRequest(correlation_id="c1"))
    assert result.passed
    assert result.total_files == 0
    assert result.failure_code is None


def test_seeded_reader_turns_the_ratchet_red() -> None:
    ratchet = {"alpha": 0, "beta": 0, "outside": 0}
    control, _ = handler(repositories(), ratchet)
    assert control.handle(ModelCensusRequest(correlation_id="control")).passed

    seeded = repositories()
    seeded["alpha"]["src/seed.py"] = READER
    census, _ = handler(seeded, ratchet)
    result = census.handle(ModelCensusRequest(correlation_id="seeded"))
    assert not result.passed
    assert result.failure_code == "ratchet-grew"
    assert "alpha=1>0" in result.failure_detail


def test_seeded_reader_in_a_git_fixture_fails_the_cli(tmp_path: Path) -> None:
    registry = tmp_path / "registry"
    repo = registry / "alpha"
    repo.mkdir(parents=True)
    env = scrub_git_location_env(os.environ)
    git = ["git", "-C", str(repo)]
    subprocess.run([*git, "init", "-q"], check=True, env=env)
    (repo / "ok.py").write_text(CLEAN["src/a.py"])
    subprocess.run([*git, "add", "--", "ok.py"], check=True, env=env)
    ratchet = tmp_path / "ratchet.yaml"
    ratchet.write_text("ratchet:\n  alpha: 0\n")
    document = yaml.safe_load((PACKAGE / "overlay.example.yaml").read_text())
    document["repositories"] = [{"name": "alpha", "own_line": False}]
    document["ratchet_file"] = str(ratchet)
    overlay_file = tmp_path / "overlay.yaml"
    overlay_file.write_text(yaml.safe_dump(document))
    environ = {
        "ONEX_LEDGER_CENSUS_OVERLAY": str(overlay_file),
        "ONEX_LEDGER_CENSUS_ROOT": str(registry),
    }
    assert main(["--repos", "alpha"], environ=environ) == 0
    (repo / "reader.sh").write_text('cat "$ONEX_LEDGER_PATH"\n')
    subprocess.run([*git, "add", "--", "reader.sh"], check=True, env=env)
    assert main(["--repos", "alpha"], environ=environ) == 1


def test_a_count_below_the_ratchet_passes_and_lowers_never_raises(
    tmp_path: Path,
) -> None:
    census, _ = handler(repositories(), {"alpha": 3, "beta": 0, "outside": 0})
    result = census.handle(ModelCensusRequest(correlation_id="c2"))
    assert result.passed
    assert result.lowered_ratchet == {"alpha": 0, "beta": 0, "outside": 0}
    store = YamlRatchetStore(tmp_path / "r.yaml")
    store.write(result.lowered_ratchet, "# header\n")
    assert store.read() == {"alpha": 0, "beta": 0, "outside": 0}


def test_own_line_repository_is_reported_and_left_out_of_the_total() -> None:
    repos = repositories(outside={"x.sh": 'cat "$ONEX_LEDGER_PATH"\n'})
    census, _ = handler(repos, {"alpha": 0, "beta": 0, "outside": 1})
    result = census.handle(ModelCensusRequest(correlation_id="c3"))
    assert result.passed
    assert result.total_files == 0
    assert [c.files for c in result.counts if c.own_line] == [1]


def test_the_repository_is_at_or_under_its_ratchet_row() -> None:
    """The ratchet file in this repository holds this repository's count; the live tree may not exceed it."""
    document = yaml.safe_load((PACKAGE / "overlay.example.yaml").read_text())
    table = YamlRatchetStore(RATCHET).read()
    assert set(table) == {"omnimarket"}
    source = GitRepositorySource(
        ROOT.parent, document["max_file_bytes"], {"omnimarket": str(ROOT)}
    )
    live = ModelCensusOverlay.model_validate(document)

    result = HandlerFileCensus(
        live,
        source,
        YamlRatchetStore(RATCHET),
        InMemoryEmissionSink(),
        lambda: NOW,
        "in_memory",
    ).handle(ModelCensusRequest(correlation_id="tree", repositories=("omnimarket",)))
    assert result.passed, (result.failure_code, result.failure_detail)


# ---- review findings on the census (OMN-22917): each fails on the code it names ----


def lines_hit(path: str, text: str) -> list[int]:
    return [h.line for h in executable_hits(path, text, overlay())]


def test_code_sharing_a_line_with_a_docstring_still_counts() -> None:
    text = 'class A:\n    """doc"""; path = os.environ["ONEX_LEDGER_PATH"]\n'
    assert lines_hit("a.py", text) == [2]


def test_the_docstring_text_itself_still_counts_as_none() -> None:
    text = 'def f():\n    """Reads ONEX_LEDGER_PATH.\n    ROLLING_WORK_LEDGER too.\n    """\n'
    assert lines_hit("a.py", text) == []


def test_a_python_file_with_many_comments_is_scanned_in_linear_time() -> None:
    count = 12000
    text = "".join(f"x{n} = {n}  # note {n}\n" for n in range(count))
    text += 'p = os.environ["ONEX_LEDGER_PATH"]\n'
    started = time.monotonic()
    assert lines_hit("big.py", text) == [count + 1]
    assert time.monotonic() - started < 3.0


def test_a_name_inside_a_block_comment_counts_as_none() -> None:
    one_line = "/* ROLLING_WORK_LEDGER */ const x = 1;\n"
    many_lines = "/*\n * read ONEX_LEDGER_PATH here\n */\nconst x = 1;\n"
    assert lines_hit("a.js", one_line) == []
    assert lines_hit("a.js", many_lines) == []


def test_code_after_a_block_comment_and_a_string_with_a_marker_still_count() -> None:
    assert lines_hit("a.js", "/* c */ const p = process.env.ONEX_LEDGER_PATH;\n") == [1]
    assert lines_hit("a.js", 'const u = "http://x"; read(ONEX_LEDGER_PATH);\n') == [1]


def git_repo_with_file(tmp_path: Path, name: str, text: str) -> Path:
    registry = tmp_path / "registry"
    repo = registry / "alpha"
    repo.mkdir(parents=True)
    subprocess.run(
        ["git", "-C", str(repo), "init", "-q"],
        check=True,
        env=scrub_git_location_env(os.environ),
    )
    (repo / name).write_text(text, encoding="utf-8")
    subprocess.run(
        ["git", "-C", str(repo), "add", "--", name],
        check=True,
        env=scrub_git_location_env(os.environ),
    )
    return registry


def test_a_file_whose_name_git_would_quote_is_still_a_candidate_and_readable(
    tmp_path: Path,
) -> None:
    name = "r\u00e9ader tab\u00e9.sh"
    registry = git_repo_with_file(tmp_path, name, 'cat "$ONEX_LEDGER_PATH"\n')
    source = GitRepositorySource(registry, 100000)
    candidates = source.candidates("alpha", ("ONEX_LEDGER_PATH",))
    assert candidates == (name,)
    assert source.read("alpha", candidates[0]) == 'cat "$ONEX_LEDGER_PATH"\n'


def test_a_lowering_that_raced_another_lowering_never_raises_a_row(
    tmp_path: Path,
) -> None:
    path = tmp_path / "ratchet.yaml"
    store = YamlRatchetStore(path)
    store.write({"alpha": 5, "beta": 2}, "# h\n")
    snapshot_read = store.read()
    store.write({"alpha": 3, "beta": 2}, "# h\n")  # another run lowered alpha meanwhile
    store.write({**snapshot_read, "beta": 1}, "# h\n")  # this run still holds alpha=5
    assert store.read() == {"alpha": 3, "beta": 1}


def test_a_write_that_dies_midway_leaves_the_ratchet_file_intact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "ratchet.yaml"
    store = YamlRatchetStore(path)
    store.write({"alpha": 5}, "# h\n")
    before = path.read_text(encoding="utf-8")
    real = Path.write_text

    def dying(self: Path, data: str, *args: object, **kwargs: object) -> int:
        real(self, data[: len(data) // 2], *args, **kwargs)  # type: ignore[arg-type]
        raise OSError("disk full")

    monkeypatch.setattr(Path, "write_text", dying)
    with pytest.raises(OSError, match="disk full"):
        store.write({"alpha": 4}, "# h\n")
    monkeypatch.undo()
    assert path.read_text(encoding="utf-8") == before
    assert [p.name for p in tmp_path.iterdir()] == ["ratchet.yaml"]
