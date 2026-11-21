# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""AC3: the command the review workflow calls: typed arguments, JSON on stdout, documented exits."""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

from omnimarket.nodes.node_review_lock_effect.handlers.adapter_ledger import (
    CommandLedgerRowWriter,
    FileLedgerStatusReader,
)
from omnimarket.nodes.node_review_lock_effect.handlers.ledger_rows import parse_row
from omnimarket.nodes.node_review_lock_effect.runtime_review_lock import main

HERE = Path(__file__).parent
FIXTURES = HERE / "fixtures"
STUB = HERE / "ledger_append_stub.py"
FIXED = datetime(2026, 11, 17, 16, 0, 0, tzinfo=UTC)


def _env(tmp_path: Path) -> dict[str, str]:
    return {"ONEX_LEDGER_APPEND_CMD": f"{sys.executable} {STUB}", "HOME": str(tmp_path)}


def _ledger(tmp_path: Path) -> Path:
    ledger = tmp_path / "ledger_copy.md"
    ledger.write_text(
        (FIXTURES / "pr6047_verdict_rows.txt").read_text(), encoding="utf-8"
    )
    return ledger


def _acquire(
    tmp_path: Path, tag: str, head: str, capsys: pytest.CaptureFixture[str], *extra: str
) -> tuple[int, dict[str, object]]:
    code = main(
        [
            "acquire", "--repo", "omnibase_infra", "--pr", "6047", "--head", head, "--tag", tag,
            "--ledger", str(_ledger_path(tmp_path)), *extra,
        ],
        environ=_env(tmp_path),
        clock=lambda: FIXED,
    )  # fmt: skip
    out = capsys.readouterr()
    return code, json.loads(out.out)


def _ledger_path(tmp_path: Path) -> Path:
    path = tmp_path / "ledger_copy.md"
    return path if path.exists() else _ledger(tmp_path)


def test_acquire_writes_one_row_then_refuses_with_exit_3(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    before = _ledger_path(tmp_path).read_text().count("\n")
    code, first = _acquire(tmp_path, "1500", "a" * 40, capsys)
    assert code == 0
    assert first["outcome"] == "acquired"
    assert first["exit_code"] == 0
    text = _ledger_path(tmp_path).read_text()
    assert text.count("\n") == before + 1
    assert "lane=reviewlock-omnibase_infra-6047-1500" in text.splitlines()[-1]
    code, second = _acquire(tmp_path, "1501", "a" * 12, capsys)
    assert code == 3
    assert second["outcome"] == "refused_lock"
    pointer = second["pointer"]
    assert isinstance(pointer, dict)
    assert (
        pointer["pointer"]
        == "ledger:2026-11-17T16:00:00Z-reviewlock-omnibase_infra-6047-1500"
    )
    assert _ledger_path(tmp_path).read_text().count("\n") == before + 1


def test_acquire_on_a_reviewed_head_is_refused_with_the_verdict_pointer(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, result = _acquire(tmp_path, "1500", "7ce37985d13c", capsys)
    assert code == 3
    assert result["outcome"] == "refused_verdict"
    pointer = result["pointer"]
    assert isinstance(pointer, dict)
    assert (
        pointer["pointer"]
        == "ledger:2026-11-17T11:48:44Z-fable-omnibase_infra-6047-1139"
    )


def test_writer_refusal_is_exit_4_and_no_lock(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    before = _ledger_path(tmp_path).read_text()
    # The stub writer refuses any row whose text holds REFUSE, and the lock row carries the tag.
    code, result = _acquire(tmp_path, "REFUSE1", "b" * 12, capsys)
    assert code == 4
    assert result["outcome"] == "undecided"
    assert "exit 1" in str(result["detail"])
    assert _ledger_path(tmp_path).read_text() == before


def test_unreadable_ledger_is_exit_4(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = main(
        ["acquire", "--repo", "r", "--pr", "1", "--head", "c" * 12, "--tag", "1",
         "--ledger", str(tmp_path / "missing.md")],
        environ=_env(tmp_path),
    )  # fmt: skip
    assert code == 4
    assert json.loads(capsys.readouterr().out)["outcome"] == "undecided"


def test_missing_writer_command_is_exit_2(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ledger = _ledger_path(tmp_path)
    argv = ["acquire", "--repo", "r", "--pr", "1", "--head", "c" * 12, "--tag", "1",
            "--ledger", str(ledger)]  # fmt: skip
    assert main(argv, environ={}) == 2
    assert "ONEX_LEDGER_APPEND_CMD" in capsys.readouterr().err
    assert ledger.read_text() == (FIXTURES / "pr6047_verdict_rows.txt").read_text()


def test_row_parser_keeps_the_first_of_a_repeated_cell_and_the_free_text() -> None:
    row = parse_row(
        "2026-11-17T13:06:58Z | STATUS | lane=fable-x-1-1310 | via=a | head=0b0a4b819f86 | via=b | the text"
    )
    assert row is not None
    assert row.cells["via"] == "a"
    assert row.text == "the text"
    assert parse_row("not a ledger row") is None


def test_bad_arguments_are_exit_2(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["acquire", "--repo", "r"], environ=_env(tmp_path)) == 2
    assert main(["acquire", "--repo", "r", "--pr", "1", "--head", "zz", "--tag", "1",
                 "--ledger", str(tmp_path / "l.md")], environ=_env(tmp_path)) == 2  # fmt: skip
    assert main(["acquire", "--repo", "r", "--pr", "1", "--head", "c" * 12, "--tag", "1"],
                environ={}) == 2  # fmt: skip
    capsys.readouterr()


def test_the_command_runs_as_a_module_with_the_documented_exit_codes(
    tmp_path: Path,
) -> None:
    ledger = _ledger(tmp_path)
    env = {**_env(tmp_path), "PATH": "/usr/bin:/bin"}
    argv = [
        sys.executable, "-m", "omnimarket.nodes.node_review_lock_effect", "acquire",
        "--repo", "omnibase_infra", "--pr", "6047", "--head", "fca8c3ea6891", "--tag", "1500",
        "--ledger", str(ledger),
    ]  # fmt: skip
    done = subprocess.run(argv, capture_output=True, text=True, env=env, check=False)
    assert done.returncode == 3, done.stderr
    assert json.loads(done.stdout)["outcome"] == "refused_verdict"
    assert done.stderr.startswith("REVIEW-LOCK refused_verdict")


def test_check_scope_cli_holds_then_releases_on_the_6047_file_lists(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    base = [
        "check-scope",
        "--first-files", str(FIXTURES / "pr6047_files_7ce37985d13c.txt"),
        "--new-files", str(FIXTURES / "pr6047_files_fca8c3ea6891.txt"),
    ]  # fmt: skip
    code = main([*base, "--comment", "re-review please"])
    held = json.loads(capsys.readouterr().out)
    assert code == 3
    assert held["verdict"] == "hold"
    assert len(held["unnamed_files"]) == 6
    comment = tmp_path / "comment.md"
    comment.write_text(
        "\n".join(f"- {f}: finding F{i}" for i, f in enumerate(held["added_files"], 1))
        + "\n"
    )
    code = main([*base, "--comment-file", str(comment)])
    released = json.loads(capsys.readouterr().out)
    assert code == 0
    assert released["verdict"] == "release"
    assert released["unnamed_files"] == []


def test_check_scope_reads_the_file_sets_from_a_local_clone(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = tmp_path / "clone"
    repo.mkdir()

    def git(*args: str) -> str:
        done = subprocess.run(
            [
                "git",
                "-C",
                str(repo),
                "-c",
                "user.name=t",
                "-c",
                "user.email=t@t",
                *args,
            ],
            capture_output=True,
            text=True,
            check=True,
            env={"PATH": "/usr/bin:/bin:/usr/local/bin", "HOME": str(tmp_path)},
        )
        return done.stdout.strip()

    git("init", "-q", "-b", "main")
    (repo / "a.py").write_text("a\n")
    git("add", "a.py")
    git("commit", "-q", "-m", "base")
    base = git("rev-parse", "HEAD")
    (repo / "b.py").write_text("b\n")
    git("add", "b.py")
    git("commit", "-q", "-m", "first")
    first = git("rev-parse", "HEAD")
    (repo / "c.py").write_text("c\n")
    git("add", "c.py")
    git("commit", "-q", "-m", "fixer")
    new = git("rev-parse", "HEAD")
    args = ["check-scope", "--repo-dir", str(repo), "--base", base, "--first-head", first,
            "--new-head", new]  # fmt: skip
    assert main([*args, "--comment", "nothing"]) == 3
    held = json.loads(capsys.readouterr().out)
    assert held["added_files"] == ["c.py"]
    assert main([*args, "--comment", "- c.py: finding F1"]) == 0
    capsys.readouterr()
    assert main(["check-scope", "--repo-dir", str(repo)]) == 2
    capsys.readouterr()


def test_check_scope_without_inputs_is_exit_2(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main(["check-scope"]) == 2
    capsys.readouterr()


def test_file_reader_skips_archive_rolls_outside_the_window(tmp_path: Path) -> None:
    archive = tmp_path / "archive"
    archive.mkdir()
    row = (FIXTURES / "pr6047_verdict_rows.txt").read_text().splitlines()[0]
    (archive / "ROLLING_WORK_LEDGER_2026-11-16-split.md").write_text(row + "\n")
    (archive / "ROLLING_WORK_LEDGER_2026-08-29-split.md").write_text(
        row.replace("1139", "0001") + "\n"
    )
    live = tmp_path / "ledger_copy.md"
    live.write_text("")
    reader = FileLedgerStatusReader(live, clock=lambda: FIXED)
    lanes = [r.lane for r in reader.status_rows("omnibase_infra")]
    assert lanes == ["fable-omnibase_infra-6047-1139"]


def test_command_writer_passes_the_ledger_and_the_row_as_arguments(
    tmp_path: Path,
) -> None:
    ledger = tmp_path / "L.md"
    ledger.write_text("")
    writer = CommandLedgerRowWriter([sys.executable, str(STUB)], ledger)
    writer.append("2026-11-17T16:00:00Z | STATUS | lane=x")
    assert ledger.read_text() == "2026-11-17T16:00:00Z | STATUS | lane=x\n"
    with pytest.raises(OSError, match="exit 1"):
        writer.append("REFUSE")
    with pytest.raises(OSError, match="did not run"):
        CommandLedgerRowWriter(["/nonexistent/ledger-writer"], ledger).append("x")
