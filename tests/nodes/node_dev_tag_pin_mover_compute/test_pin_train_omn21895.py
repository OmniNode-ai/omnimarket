# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-21895: the nightly sibling-pin train resolves its candidate before it opens a PR.

The recorded conflict is omnimarket's pair: omnibase_infra pinned by a dev tag
whose commit names its own omnibase-core dev tag, so a core-only move fails the
Sibling Release Compatibility Gate with "Requirements contain conflicting URLs
for package omnibase-core". The rows below are the ``INFRA_CORE_TAGS`` of
tests/ci/test_sibling_core_pin_pair_omn17427.py (a test pins them to that file)
and the error sentence is that file's own.

Every refusal has a positive control: the same fixture, one tag set that
co-resolves, is accepted.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

from omnimarket.nodes.node_dev_tag_pin_mover_compute.handlers.handler_dev_tag_pin_set import (
    HandlerDevTagPinSet,
    render_pin_set_body,
)
from omnimarket.nodes.node_dev_tag_pin_mover_compute.handlers.handler_dev_tag_pin_train import (
    HandlerDevTagPinTrain,
    ModelTrainPorts,
)
from omnimarket.nodes.node_dev_tag_pin_mover_compute.models import (
    EnumPinSetOutcome,
    EnumTrainStatus,
    ModelPinSetCandidate,
    ModelPinSetRequest,
    ModelPinSetSibling,
    ModelResolveOutcome,
)
from omnimarket.nodes.node_dev_tag_pin_mover_compute.train_sources import (
    make_publish,
    make_scratch_resolve,
)

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[3]
PAIR_TEST = REPO_ROOT / "tests" / "ci" / "test_sibling_core_pin_pair_omn17427.py"
INFRA_URL = "https://github.com/OmniNode-ai/omnibase_infra.git"
CORE_URL = "https://github.com/OmniNode-ai/omnibase_core.git"

# The two INFRA_CORE_TAGS rows: omnibase_infra commit -> its omnibase-core tag.
OLD_INFRA_SHA, OLD_CORE = "091013fd7adb", "dev-20261030152335-66d35d2283c3"
NEW_INFRA_SHA, NEW_CORE = "4284f5bf5587", "dev-20261031000524-fc6159b98026"
OLD_INFRA = f"dev-20261030160000-{OLD_INFRA_SHA}"
NEW_INFRA = f"dev-20261031054837-{NEW_INFRA_SHA}"

PYPROJECT = (
    "[tool.uv.sources]\n"
    f'omnibase-infra = {{ git = "{INFRA_URL}", tag = "{OLD_INFRA}" }}\n'
    f'omnibase-core = {{ git = "{CORE_URL}", tag = "{OLD_CORE}" }}\n'
)
# The Sibling Release Compatibility Gate's sentence, from the pair test.
RECORDED_DETAIL = (
    f"omnibase_infra {OLD_INFRA_SHA} pins omnibase-core {OLD_CORE}, "
    f"omnimarket pins {NEW_CORE}; move both pins in one change"
)


def _core(*, newer: bool = True) -> ModelPinSetSibling:
    candidates = (ModelPinSetCandidate(tag=NEW_CORE, green=True),) if newer else ()
    return ModelPinSetSibling(
        distribution="omnibase-core",
        repository_url=CORE_URL,
        current_tag=OLD_CORE,
        candidates=candidates,
    )


def _infra(*, newest_green: bool | None) -> ModelPinSetSibling:
    """omnibase_infra at the recorded old commit; its newest tag names the new core."""
    return ModelPinSetSibling(
        distribution="omnibase-infra",
        repository_url=INFRA_URL,
        current_tag=OLD_INFRA,
        current_requires={"omnibase-core": OLD_CORE},
        candidates=(
            ModelPinSetCandidate(
                tag=NEW_INFRA, green=newest_green, requires={"omnibase-core": NEW_CORE}
            ),
        ),
    )


def _request(*siblings: ModelPinSetSibling) -> ModelPinSetRequest:
    return ModelPinSetRequest(consumer="omnimarket", siblings=siblings)


class _Ports:
    """Records the order of the train's effects; resolve and publish are scripted."""

    def __init__(
        self, request: ModelPinSetRequest, resolved: ModelResolveOutcome
    ) -> None:
        self.calls: list[str] = []
        self.published: list[tuple[str, str, str]] = []
        self._request, self._resolved = request, resolved

    def ports(self) -> ModelTrainPorts:
        return ModelTrainPorts(
            gather=self._gather, resolve=self._resolve, publish=self._publish
        )

    def _gather(self, pyproject_text: str) -> ModelPinSetRequest:
        self.calls.append("gather")
        return self._request

    def _resolve(self, candidate: str) -> ModelResolveOutcome:
        self.calls.append("resolve")
        return self._resolved

    def _publish(self, pyproject_text: str, lock_text: str, body: str) -> str:
        self.calls.append("publish")
        self.published.append((pyproject_text, lock_text, body))
        return "created"


def _train(fake: _Ports, *, publish: bool = True) -> Any:
    return HandlerDevTagPinTrain().handle(
        pyproject_text=PYPROJECT,
        lock_text="# lock\n",
        ports=fake.ports(),
        publish_enabled=publish,
    )


def test_the_rows_are_the_ones_the_pair_test_records() -> None:
    text = PAIR_TEST.read_text(encoding="utf-8")
    assert f'("{OLD_INFRA_SHA}", "{OLD_CORE}")' in text
    assert f'("{NEW_INFRA_SHA}", "{NEW_CORE}")' in text
    assert "move both pins in one change" in text


def test_replay_conflicting_core_urls_refuses_the_core_only_candidate() -> None:
    """Infra has no green newer tag, core does: a core-only move is the recorded conflict."""
    fake = _Ports(
        _request(_core(), _infra(newest_green=False)),
        ModelResolveOutcome(ok=True, lock_text="# resolved\n"),
    )
    report, pyproject, lock = _train(fake)

    assert report.status is EnumTrainStatus.SKIPPED_NO_CO_RESOLVING_SET
    assert report.selection.outcome is EnumPinSetOutcome.NO_CO_RESOLVING_SET
    conflict = report.selection.conflicts[0]
    assert conflict.package == "omnibase-core"
    assert conflict.urls == (f"{CORE_URL}?tag={NEW_CORE}", f"{CORE_URL}?tag={OLD_CORE}")
    assert conflict.detail == RECORDED_DETAIL
    # The conflicting URLs are printed on the skipped row.
    assert "Requirements contain conflicting URLs for package `omnibase-core`" in (
        report.skipped_row
    )
    for url in conflict.urls:
        assert f"- {url}" in report.skipped_row
    # No candidate was locked, nothing was pushed, nothing was written.
    assert fake.calls == ["gather"]
    assert fake.published == []
    assert (pyproject, lock) == (PYPROJECT, "# lock\n")


def test_sibling_pins_move_together() -> None:
    """Positive control for the replay: infra's newest tag is green, so core follows it."""
    selection = HandlerDevTagPinSet().handle(
        _request(_core(), _infra(newest_green=True))
    )
    assert selection.outcome is EnumPinSetOutcome.MOVED
    assert {m.distribution: m.new_tag for m in selection.moves} == {
        "omnibase-core": NEW_CORE,
        "omnibase-infra": NEW_INFRA,
    }
    infra_move = next(m for m in selection.moves if m.distribution == "omnibase-infra")
    assert infra_move.requires == {"omnibase-core": NEW_CORE}
    assert selection.held_back == ()

    fake = _Ports(
        _request(_core(), _infra(newest_green=True)),
        ModelResolveOutcome(ok=True, lock_text="# resolved\n"),
    )
    report, pyproject, lock = _train(fake)
    assert report.status is EnumTrainStatus.PUBLISHED
    assert f'tag = "{NEW_CORE}"' in pyproject
    assert f'tag = "{NEW_INFRA}"' in pyproject
    assert OLD_CORE not in pyproject
    assert OLD_INFRA not in pyproject
    assert lock == "# resolved\n"
    # One PR body lists every moved pin and says they moved together.
    assert fake.published == [(pyproject, "# resolved\n", report.body)]
    for tag in (OLD_CORE, NEW_CORE, OLD_INFRA, NEW_INFRA):
        assert f"`{tag}`" in report.body
    assert "Moved together" in report.body


def test_a_dependency_tag_that_infra_does_not_name_is_not_taken() -> None:
    """Core has two newer green tags; only the one infra names is chosen."""
    newest_core = "dev-20261031120000-aaaaaaaaaaaa"
    core = ModelPinSetSibling(
        distribution="omnibase-core",
        repository_url=CORE_URL,
        current_tag=OLD_CORE,
        candidates=(
            ModelPinSetCandidate(tag=newest_core, green=True),
            ModelPinSetCandidate(tag=NEW_CORE, green=True),
        ),
    )
    selection = HandlerDevTagPinSet().handle(_request(core, _infra(newest_green=True)))
    assert {m.distribution: m.new_tag for m in selection.moves} == {
        "omnibase-core": NEW_CORE,
        "omnibase-infra": NEW_INFRA,
    }
    core_move = next(m for m in selection.moves if m.distribution == "omnibase-core")
    assert core_move.rolled == (NEW_CORE,)
    assert render_pin_set_body(selection).count("->") == 2


def test_resolve_before_open_never_reaches_publish_when_the_lock_fails() -> None:
    urls = (f"{CORE_URL}?tag={NEW_CORE}", f"{CORE_URL}?tag={OLD_CORE}")
    fake = _Ports(
        _request(_core(), _infra(newest_green=True)),
        ModelResolveOutcome(
            ok=False,
            stderr="Requirements contain conflicting URLs for package `omnibase-core`:",
            conflicting_urls=urls,
        ),
    )
    report, pyproject, lock = _train(fake)
    assert report.status is EnumTrainStatus.SKIPPED_RESOLVE_FAILED
    assert fake.calls == ["gather", "resolve"]
    assert fake.published == []
    assert (pyproject, lock) == (PYPROJECT, "# lock\n")
    for url in urls:
        assert f"- {url}" in report.skipped_row


def test_resolve_before_open_orders_the_effects() -> None:
    fake = _Ports(
        _request(_core(), _infra(newest_green=True)),
        ModelResolveOutcome(ok=True, lock_text="# resolved\n"),
    )
    _train(fake)
    assert fake.calls == ["gather", "resolve", "publish"]
    # A dry run resolves too, and opens nothing.
    dry = _Ports(
        _request(_core(), _infra(newest_green=True)),
        ModelResolveOutcome(ok=True, lock_text="# resolved\n"),
    )
    report, _, _ = _train(dry, publish=False)
    assert report.status is EnumTrainStatus.DRY_RUN
    assert dry.calls == ["gather", "resolve"]


def test_resolve_before_open_scratch_lock_parses_the_conflicting_urls(
    tmp_path: Path,
) -> None:
    (tmp_path / "uv.lock").write_text("# base\n")
    stderr = (
        "  x No solution found when resolving dependencies:\n"
        "Requirements contain conflicting URLs for package `omnibase-core`:\n"
        f"- {CORE_URL}?tag={NEW_CORE}\n"
        f"- {CORE_URL}?tag={OLD_CORE}\n"
    )
    seen: list[Path] = []

    def failing(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        seen.append(kwargs["cwd"])
        return subprocess.CompletedProcess(argv, 2, "", stderr)

    outcome = make_scratch_resolve(tmp_path, run=failing)(PYPROJECT)
    assert not outcome.ok
    assert outcome.conflicting_urls == (
        f"{CORE_URL}?tag={NEW_CORE}",
        f"{CORE_URL}?tag={OLD_CORE}",
    )
    # The lock ran in a scratch copy, never in the consumer's tree.
    assert seen
    assert seen[0] != tmp_path
    assert (tmp_path / "pyproject.toml").exists() is False

    def passing(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        (kwargs["cwd"] / "uv.lock").write_text("# resolved\n")
        return subprocess.CompletedProcess(argv, 0, "", "")

    ok = make_scratch_resolve(tmp_path, run=passing)(PYPROJECT)
    assert ok.ok
    assert ok.lock_text == "# resolved\n"


def test_one_pr_per_night_over_three_new_dev_tags(tmp_path: Path) -> None:
    """Three new core tags roll into one move; a second run updates the PR in place."""
    tags = [
        "dev-20261101010000-111111111111",
        "dev-20261101020000-222222222222",
        "dev-20261101030000-333333333333",
    ]
    core = ModelPinSetSibling(
        distribution="omnibase-core",
        repository_url=CORE_URL,
        current_tag=OLD_CORE,
        candidates=tuple(
            ModelPinSetCandidate(tag=t, green=True) for t in reversed(tags)
        ),
    )
    selection = HandlerDevTagPinSet().handle(_request(core))
    assert [(m.distribution, m.new_tag) for m in selection.moves] == [
        ("omnibase-core", tags[2])
    ]
    assert selection.moves[0].rolled == tuple(reversed(tags))

    repo = tmp_path / "consumer"
    repo.mkdir()
    open_prs: list[str] = []
    commands: list[list[str]] = []

    def fake_run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        commands.append(argv)
        out = ""
        if argv[:3] == ["gh", "pr", "list"]:
            out = "42\n" if open_prs else ""
        elif argv[:3] == ["gh", "pr", "create"]:
            open_prs.append("42")
        return subprocess.CompletedProcess(argv, 0, out, "")

    publish = make_publish(
        repo,
        branch="bot/omn-21895-sibling-pin-train",
        base="dev",
        title="t",
        commit_message="m",
        run=fake_run,
    )
    first = publish(PYPROJECT, "# lock\n", render_pin_set_body(selection))
    second = publish(PYPROJECT, "# lock\n", render_pin_set_body(selection))

    assert (first, second) == ("created", "updated #42")
    verbs = [" ".join(c[:3]) for c in commands if c[0] == "gh"]
    assert verbs.count("gh pr create") == 1
    assert verbs.count("gh pr edit") == 1
    pushes = [c for c in commands if "push" in c]
    assert len(pushes) == 2
    assert all(
        c[-3:] == ["--force", "origin", "bot/omn-21895-sibling-pin-train"][-3:]
        for c in pushes
    )
    # Every night resolves before it publishes; the body names all three tags.
    assert all(f"`{t}`" in render_pin_set_body(selection) for t in tags)


CHANGED_FILES = (
    ".github/workflows/sibling-pin-train.yml",
    "contracts/OMN-21895.yaml",
    "src/omnimarket/nodes/node_dev_tag_pin_mover_compute/contract.yaml",
    "src/omnimarket/nodes/node_dev_tag_pin_mover_compute/handlers/handler_dev_tag_pin_set.py",
    "src/omnimarket/nodes/node_dev_tag_pin_mover_compute/handlers/handler_dev_tag_pin_train.py",
    "src/omnimarket/nodes/node_dev_tag_pin_mover_compute/models/__init__.py",
    "src/omnimarket/nodes/node_dev_tag_pin_mover_compute/models/model_dev_tag_pin_set.py",
    "src/omnimarket/nodes/node_dev_tag_pin_mover_compute/models/model_dev_tag_pin_train.py",
    "src/omnimarket/nodes/node_dev_tag_pin_mover_compute/runtime_dev_tag_pin_train.py",
    "src/omnimarket/nodes/node_dev_tag_pin_mover_compute/train_sources.py",
    "tests/nodes/node_dev_tag_pin_mover_compute/test_pin_train_omn21895.py",
)


def test_gates_unchanged_over_the_changed_file_list() -> None:
    for path in CHANGED_FILES:
        assert (REPO_ROOT / path).is_file(), path
        assert not path.startswith(".onex_ratchets/"), path
        assert path != ".github/workflows/ci.yml", path
        assert "allowlist" not in path, path
        assert "baseline" not in path, path
    workflow = yaml.safe_load(
        (REPO_ROOT / ".github/workflows/sibling-pin-train.yml").read_text()
    )
    triggers = workflow.get("on", workflow.get(True))
    # Schedule and workflow_dispatch only: no pull_request context is minted.
    assert set(triggers) == {"schedule", "workflow_dispatch"}
    assert triggers["workflow_dispatch"]["inputs"]["dry_run"]["type"] == "boolean"
    assert re.fullmatch(r"\d+ \d+ \* \* \*", triggers["schedule"][0]["cron"])
    # The node's gates stay: it is still a pure COMPUTE node with no bus topics.
    contract = yaml.safe_load(
        (
            REPO_ROOT
            / "src/omnimarket/nodes/node_dev_tag_pin_mover_compute/contract.yaml"
        ).read_text()
    )
    assert contract["event_bus"] == {"subscribe_topics": [], "publish_topics": []}
    operations = [h["operation"] for h in contract["handler_routing"]["handlers"]]
    assert operations == [
        "move_dev_tag_pin",
        "select_dev_tag_pin_set",
        "run_dev_tag_pin_train",
    ]
