# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The liveness read node (OMN-23529): golden and error chain events, every overlay key required, and the command reader.

The golden chain is a made read naming an undeclared timer, published on the overlay's completed topic with the fact the landing
decision reads. The error chain is a read that cannot be made, published on the failed topic, with an unreadable fact that names
no file. The overlay test removes one key at a time and the refusal must name it. The command reader runs against a real git
checkout and a stand-in reader command, so the head proof, the base diff filter, the timeout and the exit codes are measured.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
import yaml
from omnibase_core.validators.no_unguarded_git_subprocess import (
    scrub_git_location_env,
)

from omnimarket.models.landing_decision import EnumLivenessRead
from omnimarket.nodes.node_pr_landing_liveness_read_effect.handlers.adapter_command_reader import (
    CommandLivenessReader,
)
from omnimarket.nodes.node_pr_landing_liveness_read_effect.handlers.handler_pr_landing_liveness_read import (
    HandlerPrLandingLivenessRead,
)
from omnimarket.nodes.node_pr_landing_liveness_read_effect.handlers.liveness_read_errors import (
    LivenessReadError,
)
from omnimarket.nodes.node_pr_landing_liveness_read_effect.handlers.liveness_read_ports import (
    InMemoryLivenessReader,
    InMemoryLivenessReadPublisher,
)
from omnimarket.nodes.node_pr_landing_liveness_read_effect.models.model_liveness_read import (
    ModelLivenessReadCompleted,
    ModelLivenessReadFailed,
    ModelLivenessReadRequest,
)
from omnimarket.nodes.node_pr_landing_liveness_read_effect.overlay import (
    ModelLivenessReadOverlay,
    load_overlay,
)

pytestmark = pytest.mark.unit

EXAMPLE = (
    Path(__file__).resolve().parents[4]
    / "src/omnimarket/nodes/node_pr_landing_liveness_read_effect/overlay.example.yaml"
)
FIXED_ID = UUID("00000000-0000-4000-8000-000000000023")
REPO = "OmniNode-ai/omnimarket"
PR = f"{REPO}#9001"
HEAD = "7265930bafc673e66ced5bb67b9464bc460f1d77"
TIMER = "deploy/systemd/onex-new.timer"
LEAF_KEYS = (
    ("reader", "backend"),
    ("reader", "argv"),
    ("checkout", "internal_root_env"),
    ("checkout", "registry_root_env"),
    ("checkout", "base_ref"),
    ("timeouts", "read_seconds"),
    ("timeouts", "git_seconds"),
    ("topics", "completed"),
    ("topics", "failed"),
)


def _document() -> dict[str, Any]:
    loaded = yaml.safe_load(EXAMPLE.read_text())
    assert isinstance(loaded, dict)
    return loaded


def _overlay(**sections: dict[str, Any]) -> ModelLivenessReadOverlay:
    doc = _document()
    for name, values in sections.items():
        doc[name] = {**doc[name], **values}
    return load_overlay(explicit=doc)


def _request(head: str = HEAD) -> ModelLivenessReadRequest:
    return ModelLivenessReadRequest(correlation_id=FIXED_ID, pr=PR, head_sha=head)


def test_golden_chain_a_read_naming_an_undeclared_timer_publishes_the_completed_event() -> (
    None
):
    overlay = _overlay()
    publisher = InMemoryLivenessReadPublisher()
    handler = HandlerPrLandingLivenessRead(
        overlay, InMemoryLivenessReader({(REPO, HEAD): (TIMER,)}), publisher
    )
    result = handler.handle(_request())
    assert result.liveness.read is EnumLivenessRead.READ
    assert result.liveness.undeclared_sources == (TIMER,)
    assert result.liveness.head_sha == HEAD
    assert result.topic == overlay.topics.completed
    assert len(publisher.published) == 1
    topic, event = publisher.published[0]
    assert topic == overlay.topics.completed
    assert isinstance(event, ModelLivenessReadCompleted)
    assert event.liveness == result.liveness
    assert event.correlation_id == FIXED_ID


def test_golden_chain_a_read_that_finds_nothing_names_no_file() -> None:
    publisher = InMemoryLivenessReadPublisher()
    result = HandlerPrLandingLivenessRead(
        _overlay(), InMemoryLivenessReader({(REPO, HEAD): ()}), publisher
    ).handle(_request())
    assert result.liveness.read is EnumLivenessRead.READ
    assert result.liveness.undeclared_sources == ()
    assert [t for t, _ in publisher.published] == [_overlay().topics.completed]


def test_error_chain_an_unreadable_read_publishes_the_failed_event_and_names_no_file() -> (
    None
):
    overlay = _overlay()
    publisher = InMemoryLivenessReadPublisher()
    handler = HandlerPrLandingLivenessRead(
        overlay,
        InMemoryLivenessReader({}, unreadable={(REPO, HEAD): "the reader timed out"}),
        publisher,
    )
    result = handler.handle(_request())
    assert result.liveness.read is EnumLivenessRead.UNREADABLE
    assert result.liveness.undeclared_sources == ()
    assert result.liveness.detail == "the reader timed out"
    assert result.topic == overlay.topics.failed
    assert [t for t, _ in publisher.published] == [overlay.topics.failed]
    event = publisher.published[0][1]
    assert isinstance(event, ModelLivenessReadFailed)
    assert event.detail == "the reader timed out"
    assert event.head_sha == HEAD


def test_the_example_overlay_loads_with_every_key() -> None:
    overlay = load_overlay(explicit=_document())
    assert overlay.reader.backend == "command"
    assert overlay.topics.completed != overlay.topics.failed


def test_the_example_overlay_publishes_on_the_topics_the_contract_declares() -> None:
    overlay = load_overlay(explicit=_document())
    contract = yaml.safe_load((EXAMPLE.parent / "contract.yaml").read_text())
    completed = "onex.evt.omnimarket.pr-landing-liveness-read-completed.v1"
    failed = "onex.evt.omnimarket.pr-landing-liveness-read-failed.v1"
    assert overlay.topics.completed == completed
    assert overlay.topics.failed == failed
    assert contract["event_bus"]["publish_topics"] == [completed, failed]
    assert contract["terminal_event"] == completed


@pytest.mark.parametrize(("section", "key"), LEAF_KEYS)
def test_an_overlay_missing_one_key_is_refused_with_the_key_named(
    section: str, key: str
) -> None:
    doc = _document()
    del doc[section][key]
    with pytest.raises(Exception, match=key):
        load_overlay(explicit=doc)


# --- the command reader against a real checkout -------------------------------------------------


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        [
            "git",
            "-C",
            str(cwd),
            "-c",
            "user.name=lab",
            "-c",
            "user.email=lab@example.invalid",
            *args,
        ],
        capture_output=True,
        text=True,
        check=True,
        env=scrub_git_location_env(os.environ),
    ).stdout.strip()


@pytest.fixture
def checkout(tmp_path: Path) -> tuple[Path, Path, str]:
    """A registry root with a checkout of the PR's repository whose head adds one timer and one doc over ``base``."""
    registry = tmp_path / "registry"
    repo = registry / "omnimarket"
    repo.mkdir(parents=True)
    _git(repo, "init", "-q", "-b", "main")
    (repo / "old").mkdir()
    (repo / "old" / "already.timer").write_text("[Timer]\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "base")
    _git(repo, "branch", "base")
    (repo / "deploy" / "systemd").mkdir(parents=True)
    (repo / TIMER).write_text("[Timer]\nOnCalendar=hourly\n")
    (repo / "README2.md").write_text("doc\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "head")
    internal = tmp_path / "internal"
    internal.mkdir()
    return registry, internal, _git(repo, "rev-parse", "HEAD")


def _script(body: str) -> tuple[str, ...]:
    return (sys.executable, "-c", body)


FINDINGS = (
    "import sys;"
    f"print('undeclared_source: omnimarket/{TIMER}: installer code with no entry');"
    "print('undeclared_source: omnimarket/old/already.timer: a timer with no entry');"
    "print('undeclared_source: other/deploy/x.timer: a timer with no entry');"
    "print('automation-liveness overlay: 3 findings');"
    "sys.exit(1)"
)


def _reader(
    registry: Path, internal: Path, argv: tuple[str, ...], read_seconds: float = 30
) -> CommandLivenessReader:
    overlay = _overlay(
        reader={"argv": list(argv)},
        checkout={
            "internal_root_env": "LIVENESS_INTERNAL",
            "registry_root_env": "LIVENESS_REGISTRY",
            "base_ref": "base",
        },
        timeouts={"read_seconds": read_seconds},
    )
    return CommandLivenessReader(
        overlay,
        {"LIVENESS_INTERNAL": str(internal), "LIVENESS_REGISTRY": str(registry)},
    )


def test_the_command_reader_keeps_only_undeclared_sources_this_head_adds(
    checkout: tuple[Path, Path, str],
) -> None:
    registry, internal, head = checkout
    found = _reader(registry, internal, _script(FINDINGS)).read(REPO, head)
    # the base's own undeclared timer and another repository's are not this head's
    assert found == (TIMER,)


def test_the_command_reader_refuses_a_checkout_at_another_head(
    checkout: tuple[Path, Path, str],
) -> None:
    registry, internal, _ = checkout
    with pytest.raises(LivenessReadError, match="not the head"):
        _reader(registry, internal, _script(FINDINGS)).read(REPO, HEAD)


def test_the_command_reader_says_the_read_was_not_made_when_the_reader_times_out(
    checkout: tuple[Path, Path, str],
) -> None:
    registry, internal, head = checkout
    with pytest.raises(LivenessReadError, match="did not answer"):
        _reader(
            registry, internal, _script("import time; time.sleep(5)"), read_seconds=0.5
        ).read(REPO, head)


def test_the_command_reader_says_the_read_was_not_made_when_the_check_could_not_run(
    checkout: tuple[Path, Path, str],
) -> None:
    registry, internal, head = checkout
    script = _script(
        "import sys; sys.stderr.write('overlay is missing\\n'); sys.exit(2)"
    )
    with pytest.raises(LivenessReadError, match="exited 2: overlay is missing"):
        _reader(registry, internal, script).read(REPO, head)


def test_the_command_reader_names_an_unset_checkout_variable(
    checkout: tuple[Path, Path, str],
) -> None:
    registry, internal, head = checkout
    reader = _reader(registry, internal, _script(FINDINGS))
    reader._environ = {"LIVENESS_REGISTRY": str(registry)}
    with pytest.raises(LivenessReadError, match="LIVENESS_INTERNAL"):
        reader.read(REPO, head)
