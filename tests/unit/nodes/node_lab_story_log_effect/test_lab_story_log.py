# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The weekly lab story log: rows in a week become a sanitized markdown log.

Every name in the fixtures is invented. The vocabulary files are synthetic stand-ins in the gate's
own restricted YAML subset: the real vocabulary is private and is read at run time, never copied.
"""

from __future__ import annotations

import os
import shutil
from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml
from omnibase_core.validators.no_unguarded_git_subprocess import (
    scrub_git_location_env,
)

from omnimarket.nodes.node_lab_story_log_effect.handlers import hygiene
from omnimarket.nodes.node_lab_story_log_effect.handlers.handler_lab_story_log import (
    HandlerLabStoryLog,
    resolve_week,
)
from omnimarket.nodes.node_lab_story_log_effect.handlers.hygiene import (
    HygieneVocabularyError,
    LabStoryLogLeakError,
    parse_restricted_yaml,
)
from omnimarket.nodes.node_lab_story_log_effect.models import (
    ModelLabStoryLogRequest,
    ModelLabStoryLogResult,
)

FIXTURES = Path(__file__).resolve().parents[3] / "fixtures" / "lab_story_log"
NODE = (
    Path(__file__).resolve().parents[4]
    / "src/omnimarket/nodes/node_lab_story_log_effect"
)

# Every invented private token the fixtures plant, in every spelling the ledger rows use.
PRIVATE_TOKENS = (
    "orchard-private",
    "vault_internal",
    "Zelda",
    "Quillbert",
    "rig-201",
    "rig-202",
    "studio-box",
    "203.0.113.7",
    "203.0.113.8",
    "lane-secret-9",
    "/srv/secret-lab",
    "tracker.example.test",
)


def _request(tmp_path: Path, **overrides: object) -> ModelLabStoryLogRequest:
    base: dict[str, object] = {
        "week": "2026-W42",
        "ledger_paths": (FIXTURES / "ledger_week.md",),
        "hygiene_vocabulary_path": FIXTURES / "hygiene_vocabulary.yaml",
        "person_names_path": FIXTURES / "person_names.txt",
        "lab_vocabulary_path": FIXTURES / "lab_vocabulary.yaml",
        "output_root": tmp_path,
    }
    base.update(overrides)
    return ModelLabStoryLogRequest(**base)


def _run(tmp_path: Path, **overrides: object) -> ModelLabStoryLogResult:
    return HandlerLabStoryLog().handle(_request(tmp_path, **overrides))


def test_resolve_week_explicit_and_previous() -> None:
    label, start, end = resolve_week("2026-W42", None)
    assert (label, start.date().isoformat(), end.date().isoformat()) == (
        "2026-W42",
        "2026-10-12",
        "2026-10-19",
    )
    # A Sunday evening run (the host tick) writes the ISO week before the running one.
    previous = resolve_week("previous", datetime(2026, 10, 18, 22, 0, tzinfo=UTC))
    assert previous[0] == "2026-W41"
    # The year is the ISO year: 2027-01-01 belongs to 2026-W53.
    assert (
        resolve_week("previous", datetime(2027, 1, 6, 12, 0, tzinfo=UTC))[0]
        == "2026-W53"
    )
    with pytest.raises(ValueError, match="ISO week"):
        resolve_week("week 42", None)
    with pytest.raises(ValueError, match="not an ISO week"):
        resolve_week("2026-W60", None)


def test_log_lands_at_the_story_log_path(tmp_path: Path) -> None:
    result = _run(tmp_path)
    expected = tmp_path / "articles/lab-journey/story-log/2026-W42.md"
    assert result.output_path == expected
    assert result.written
    assert expected.read_text(encoding="utf-8") == result.markdown


def test_dry_run_writes_nothing(tmp_path: Path) -> None:
    result = _run(tmp_path, dry_run=True)
    assert not result.written
    assert not (tmp_path / "articles").exists()


def test_only_rows_inside_the_week_count(tmp_path: Path) -> None:
    result = _run(tmp_path)
    # The fixture holds 16 rows; the first (before the week) and the last (after it) are left out.
    assert result.rows_in_week == 14
    assert result.last_row_stamp == datetime(2026, 10, 18, 23, 59, 59, tzinfo=UTC)
    assert "Before the week" not in result.markdown
    assert "first row after the week" not in result.markdown
    assert result.rulings == 1
    assert result.friction_rows == 4
    # The twice-recorded adoption of widgets#12 counts once.
    assert result.merged_prs == 3


def test_no_private_name_survives(tmp_path: Path) -> None:
    result = _run(tmp_path)
    lowered = result.markdown.lower()
    survivors = [t for t in PRIVATE_TOKENS if t.lower() in lowered]
    assert survivors == []
    for role in (
        "a private repository",
        "a teammate",
        "a lab host",
        "a lab address",
        "a lab lane",
    ):
        assert role in result.markdown
    assert result.replacements["private_repo"] >= 2
    assert result.replacements["person"] >= 2
    assert result.replacements["address"] >= 2
    assert result.replacements["host"] >= 2
    assert result.replacements["tracker"] >= 1


def test_a_surviving_private_name_stops_the_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(hygiene.Sanitizer, "sanitize", lambda _self, text: text)
    with pytest.raises(LabStoryLogLeakError, match="survived"):
        _run(tmp_path)
    assert not (tmp_path / "articles").exists()


def test_the_operators_own_words_are_not_reproduced(tmp_path: Path) -> None:
    markdown = _run(tmp_path).markdown
    assert "Move it NOW" not in markdown
    assert "outside the range" not in markdown
    # The decision text the lane recorded beside the quote is kept.
    assert "move the code" in markdown


def test_rulings_carry_their_timestamp_and_the_prs_that_closed_them(
    tmp_path: Path,
) -> None:
    markdown = _run(tmp_path).markdown
    assert "### 2026-10-12T01:00Z: Should a private repository code move" in markdown
    ruling = markdown.split("## Themes")[0]
    assert (
        "2026-10-12T02:00Z: feat(OMN-9002): move the landing controller out of a private repository (#11) (in a private repository)"
        in ruling
    )
    assert "merge queue stops arming a draft (#12) (in widgets)" in ruling
    # OMN-9003's PR merged under another ticket and is not credited to this ruling.
    assert "(#13)" not in ruling


def test_incidents_group_by_theme_with_timestamps_and_figures(tmp_path: Path) -> None:
    markdown = _run(tmp_path).markdown
    guards = markdown.split("### Hooks, guards and refusals")[1].split("###")[0]
    assert "2 friction rows between 2026-10-13T09:00Z and 2026-10-14T09:30Z" in guards
    assert "stated cost of 5 minutes across 2 rows" in guards
    lab = markdown.split("### Lab capacity and placement")[1].split("###")[0]
    assert "1 friction row on 2026-10-14T10:00Z" in lab
    assert "stated cost of 90 minutes" in lab
    assert (
        "delegation falls through"
        in markdown.split("### Delegation and model access")[1]
    )


def test_numbers_section_counts(tmp_path: Path) -> None:
    section = _run(tmp_path).markdown.split("## The week in numbers")[1].split("##")[0]
    assert "14 rows" in section
    assert "3 pull requests merged" in section
    assert "1 lane ended blocked" in section
    # 2 + 3 lane-minutes and 1.5 lane-hours: 95 minutes across the 3 friction rows that named a cost.
    assert "add up to 95 minutes across 3 rows out of 4" in section.replace("\n", " ")


def test_vocabulary_with_no_private_repos_is_refused(tmp_path: Path) -> None:
    empty = tmp_path / "vocab.yaml"
    empty.write_text(
        'schema_version: 1\nprivate_repo_names:\nperson_names:\n  - "Zelda"\n'
    )
    with pytest.raises(HygieneVocabularyError, match="private_repo_names is empty"):
        _run(tmp_path, hygiene_vocabulary_path=empty)


def test_missing_vocabulary_is_refused(tmp_path: Path) -> None:
    with pytest.raises(HygieneVocabularyError, match="cannot read"):
        _run(tmp_path, hygiene_vocabulary_path=tmp_path / "absent.yaml")
    assert not (tmp_path / "articles").exists()


def test_restricted_yaml_keeps_backslashes_raw() -> None:
    parsed = parse_restricted_yaml('a: 1\nlist:\n  - "x\\s+y"\n', "t")
    assert parsed == {"a": "1", "list": ["x\\s+y"]}
    with pytest.raises(HygieneVocabularyError):
        parse_restricted_yaml("  - orphan\n", "t")


def test_rerunning_the_same_week_is_byte_identical(tmp_path: Path) -> None:
    first = _run(tmp_path).markdown
    second = _run(tmp_path).markdown
    assert first == second


def test_contract_declares_topics_and_entry_points() -> None:
    contract = yaml.safe_load((NODE / "contract.yaml").read_text())
    assert contract["name"] == "node_lab_story_log_effect"
    command = contract["event_bus"]["subscribe_topics"][0]
    terminal = contract["terminal_event"]
    assert command == "onex.cmd.omnimarket.lab-story-log-requested.v1"
    assert terminal == "onex.evt.omnimarket.lab-story-log-completed.v1"
    assert terminal in contract["event_bus"]["publish_topics"]
    pyproject = (NODE.parents[3] / "pyproject.toml").read_text()
    assert (
        'node_lab_story_log_effect = "omnimarket.nodes.node_lab_story_log_effect"'
        in pyproject
    )
    assert (
        'lab-story-log = "omnimarket.nodes.node_lab_story_log_effect.cli:lab_story_log_command"'
        in pyproject
    )


def test_fixture_copy_is_not_mutated(tmp_path: Path) -> None:
    shutil.copy(FIXTURES / "ledger_week.md", tmp_path / "l.md")
    before = (tmp_path / "l.md").read_bytes()
    _run(tmp_path, ledger_paths=(tmp_path / "l.md",))
    assert (tmp_path / "l.md").read_bytes() == before


def test_ledger_paths_read_only_archives_that_reach_into_the_week(
    tmp_path: Path,
) -> None:
    from omnimarket.nodes.node_lab_story_log_effect.cli import ledger_paths

    live = tmp_path / "ROLLING_WORK_LEDGER.md"
    live.write_text("")
    archive = tmp_path / "archive"
    archive.mkdir()
    for name in (
        "ROLLING_WORK_LEDGER_2026-10-05-split.md",
        "ROLLING_WORK_LEDGER_2026-10-19-split.md",
    ):
        (archive / name).write_text("")
    start = datetime(2026, 10, 12, tzinfo=UTC)
    assert ledger_paths(live, start) == (
        live,
        archive / "ROLLING_WORK_LEDGER_2026-10-19-split.md",
    )


def test_publish_commits_the_log_on_a_branch_and_opens_the_pull_request(
    tmp_path: Path,
) -> None:
    import subprocess

    from omnimarket.nodes.node_lab_story_log_effect.publish import publish_story_log

    def git(*args: str, cwd: Path) -> None:
        subprocess.run(
            ["git", "-c", "user.name=t", "-c", "user.email=t@example.test", *args],
            cwd=cwd,
            check=True,
            capture_output=True,
            env=scrub_git_location_env(os.environ),
        )

    origin = tmp_path / "origin.git"
    git("init", "--bare", "-b", "main", str(origin), cwd=tmp_path)
    clone = tmp_path / "kb"
    git("clone", str(origin), str(clone), cwd=tmp_path)
    (clone / "README.md").write_text("kb\n")
    git("add", "README.md", cwd=clone)
    git("commit", "-m", "seed", cwd=clone)
    git("push", "origin", "HEAD:main", cwd=clone)
    git("remote", "set-head", "origin", "main", cwd=clone)
    git("config", "user.name", "t", cwd=clone)
    git("config", "user.email", "t@example.test", cwd=clone)
    calls: list[list[str]] = []

    def run(args: list[str], cwd: Path | None) -> subprocess.CompletedProcess[str]:
        if args[0] == "onex":
            calls.append(args)
            return subprocess.CompletedProcess(args, 0, "", "")
        return subprocess.run(
            args,
            cwd=cwd,
            capture_output=True,
            text=True,
            check=False,
            env=scrub_git_location_env(os.environ),
        )

    outcome = publish_story_log(
        clone=clone,
        repository="OmniNode-ai/example-articles",
        worktree_root=tmp_path / "wt",
        label="2026-W42",
        markdown="# log\n",
        summary="Weekly log.",
        run=run,
    )
    assert outcome.published
    assert outcome.branch == "omn-17427-lab-story-log-2026-w42"
    pushed = subprocess.run(
        [
            "git",
            "--git-dir",
            str(origin),
            "show",
            f"{outcome.branch}:articles/lab-journey/story-log/2026-W42.md",
        ],
        capture_output=True,
        text=True,
        check=True,
        env=scrub_git_location_env(os.environ),
    )
    assert pushed.stdout == "# log\n"
    assert calls[0][:4] == [
        "onex",
        "scm",
        "pr-create",
        "OmniNode-ai/example-articles",
    ]
    assert calls[0][calls[0].index("--base") + 1] == "main"
    assert calls[0][calls[0].index("--lane") + 1] == "lab-story-log"
    assert (
        calls[0][calls[0].index("--title") + 1]
        == "docs(OMN-17427): lab story log 2026-W42"
    )
