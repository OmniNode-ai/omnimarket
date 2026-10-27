# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Pinned Git source: real objects, typed dispatch and transport parity."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
import yaml
from omnibase_core.enums.enum_context_factor import EnumContextFactor
from omnibase_core.models.context.model_context_access import ModelContextAccess
from omnibase_core.models.context.model_context_caller_scope import (
    ModelContextCallerScope,
)
from omnibase_core.models.context.model_context_source_request import (
    ModelContextSourceRequest,
)
from omnibase_core.validators.no_unguarded_git_subprocess import scrub_git_location_env
from pydantic import ValidationError

from omnimarket.nodes.node_context_source_git_effect import HandlerContextSourceGit
from omnimarket.nodes.node_context_source_git_effect.models.model_git_source import (
    ModelGitSourceOverlay,
    ModelGitSourceResult,
)

pytestmark = pytest.mark.unit
CONTRACT = (
    Path(__file__).resolve().parents[3]
    / "src/omnimarket/nodes/node_context_source_git_effect/contract.yaml"
)
NOW = datetime(2026, 10, 27, tzinfo=UTC)


def git(root: Path, *args: str) -> bytes:
    return subprocess.run(
        ["git", "-C", str(root), *args],
        check=True,
        capture_output=True,
        env=scrub_git_location_env(os.environ),
    ).stdout


@pytest.fixture
def corpus(tmp_path: Path) -> tuple[Path, str, dict[str, bytes]]:
    root = tmp_path / "corpus"
    root.mkdir()
    git(root, "init", "--initial-branch=fixture")
    files = {
        "empty.md": b"",
        "rules.yaml": b"statement: Keep every ruling.\r\n",
        "shape.md": "# Shape\nTyped λ contract.\n".encode(),
        "doc?source=one": b"one\n",
        "doc?source=two": b"two\n",
    }
    for path, data in files.items():
        (root / path).write_bytes(data)
    git(root, "add", ".")
    git(
        root,
        "-c",
        "user.name=Fixture",
        "-c",
        "user.email=fixture@example.test",
        "commit",
        "-m",
        "fixture",
    )
    pin = git(root, "rev-parse", "HEAD").decode().strip()
    (root / "rules.yaml").write_text("dirty checkout\n")
    return root, pin, files


def commit_fixture(root: Path) -> str:
    git(root, "add", ".")
    git(
        root,
        "-c",
        "user.name=Fixture",
        "-c",
        "user.email=fixture@example.test",
        "commit",
        "-m",
        "next fixture",
    )
    return git(root, "rev-parse", "HEAD").decode().strip()


def test_symlink_is_unavailable_without_following_it(
    corpus: tuple[Path, str, dict[str, bytes]],
) -> None:
    root, _, files = corpus
    (root / "link.md").symlink_to("../outside.md")
    pin = commit_fixture(root)
    result = HandlerContextSourceGit(
        overlay=overlay((root, pin, files), paths=["link.md"])
    ).handle(request(pin))
    assert result.status.value == "unavailable"
    assert not result.entries


def test_invalid_utf8_is_unavailable_without_partial_entries(
    corpus: tuple[Path, str, dict[str, bytes]],
) -> None:
    root, _, files = corpus
    (root / "binary.md").write_bytes(b"\xff\xfe\x00")
    pin = commit_fixture(root)
    result = HandlerContextSourceGit(
        overlay=overlay((root, pin, files), paths=["shape.md", "binary.md"])
    ).handle(request(pin))
    assert result.status.value == "unavailable"
    assert not result.entries


def test_oversize_corpus_returns_every_byte_without_budget(
    corpus: tuple[Path, str, dict[str, bytes]],
) -> None:
    root, _, files = corpus
    large = "# Whole source\n" + "preserve these words\n" * 20000
    (root / "large.md").write_text(large)
    pin = commit_fixture(root)
    result = HandlerContextSourceGit(
        overlay=overlay((root, pin, files), paths=["large.md"])
    ).handle(request(pin))
    assert result.status.value == "resolved"
    assert result.entries[0].text == large
    assert result.has_more is False


def test_reader_failure_does_not_leak_details(
    corpus: tuple[Path, str, dict[str, bytes]],
) -> None:
    from omnimarket.nodes.node_context_source_git_effect.handlers.git_object_reader import (
        GitSourceReadError,
    )

    class UnavailableReader:
        def read(self, *args: object, **kwargs: object) -> object:
            raise GitSourceReadError("sensitive deployment details")

    result = HandlerContextSourceGit(
        overlay=overlay(corpus), reader=UnavailableReader()
    ).handle(request(corpus[1]))
    assert result.status.value == "unavailable"
    assert "sensitive" not in result.model_dump_json()


def test_blob_integrity_is_validated() -> None:
    from omnimarket.nodes.node_context_source_git_effect.models.model_git_source import (
        ModelGitBlob,
    )

    with pytest.raises(ValidationError, match="blob hash"):
        ModelGitBlob(path="doc.md", blob_sha="0" * 40, raw_bytes=b"altered")


def test_packaged_node_cli_inmemory(
    corpus: tuple[Path, str, dict[str, bytes]], tmp_path: Path
) -> None:
    """Deploy probe: the installed node must return pinned bytes through its runtime."""
    overlay_root = tmp_path / "overlays"
    overlay_file = overlay_root / "node_context_source_git_effect" / "overlay.yaml"
    overlay_file.parent.mkdir(parents=True)
    overlay_file.write_text(yaml.safe_dump(overlay(corpus).model_dump(mode="json")))
    payload = tmp_path / "request.json"
    payload.write_text(request(corpus[1]).model_dump_json())
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            "from omnibase_core.cli.cli_commands import cli; cli()",
            "node",
            "node_context_source_git_effect",
            "--input",
            str(payload),
            "--state-root",
            str(tmp_path / "state"),
            "--backend",
            "event_bus=inmemory",
            "--output",
            "receipt",
            "--emit-socket",
            str(tmp_path / "absent.sock"),
            "--timeout",
            "30",
        ],
        env={**os.environ, "ONEX_SKILL_OVERLAY_ROOTS": str(overlay_root)},
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    receipt = json.loads(completed.stdout)
    result = ModelGitSourceResult.model_validate(receipt["result"])
    assert result.status.value == "resolved"
    assert {entry.path: entry.raw_bytes for entry in result.entries} == corpus[2]


def test_pinned_object_type_must_be_commit(
    corpus: tuple[Path, str, dict[str, bytes]],
) -> None:
    root, pin, _ = corpus
    blob_sha = git(root, "rev-parse", f"{pin}:shape.md").decode().strip()
    result = HandlerContextSourceGit(
        overlay=overlay(corpus, commit_sha=blob_sha)
    ).handle(request(blob_sha))
    assert result.status.value == "unavailable"


def test_contract_timeout_reaches_injected_reader(
    corpus: tuple[Path, str, dict[str, bytes]], tmp_path: Path
) -> None:
    from omnimarket.nodes.node_context_source_git_effect.handlers.git_object_reader import (
        GitObjectReader,
    )
    from omnimarket.nodes.node_context_source_git_effect.models.model_git_source import (
        ModelGitSnapshot,
    )

    seen: list[float] = []

    class Reader:
        def read(
            self,
            repository_path: Path,
            commit_sha: str,
            paths: tuple[str, ...],
            *,
            timeout_seconds: float,
        ) -> ModelGitSnapshot:
            seen.append(timeout_seconds)
            return GitObjectReader().read(
                repository_path, commit_sha, paths, timeout_seconds=timeout_seconds
            )

    raw = yaml.safe_load(CONTRACT.read_text())
    raw["context_source_git"]["timeout_seconds"] = 2.5
    contract = tmp_path / "contract.yaml"
    contract.write_text(yaml.safe_dump(raw))
    result = HandlerContextSourceGit(
        overlay=overlay(corpus), reader=Reader(), contract_path=contract
    ).handle(request(corpus[1]))
    assert result.status.value == "resolved"
    assert seen == [2.5]


def overlay(
    corpus: tuple[Path, str, dict[str, bytes]], **changes: object
) -> ModelGitSourceOverlay:
    root, pin, files = corpus
    source: dict[str, object] = {
        "repository": "corpus",
        "repository_path": str(root),
        "commit_sha": pin,
        "factor": "canonical_shapes",
        "paths": list(files),
        "tenant_ids": ["lab"],
        "policy_id": "fixture-v1",
        "freshness_seconds": 1,
    }
    source.update(changes)
    return ModelGitSourceOverlay.model_validate({"sources": [source]})


def request(
    pin: str, tenant: str = "lab", repository: str = "corpus"
) -> ModelContextSourceRequest:
    return ModelContextSourceRequest(
        factor=EnumContextFactor.CANONICAL_SHAPES,
        caller_scope=ModelContextCallerScope(repository=repository),
        access=ModelContextAccess(tenant_id=tenant),
        revision=pin,
    )


def test_reads_pinned_blob_bytes_stamps_and_complete_tree(
    corpus: tuple[Path, str, dict[str, bytes]],
) -> None:
    root, pin, files = corpus
    node = HandlerContextSourceGit(overlay=overlay(corpus), now=lambda: NOW)
    assert node.synchronous_execution is True
    result = node.handle(request(pin))
    assert result.status.value == "resolved"
    assert set(result.tree_paths) == set(files)
    assert len(result.entries) == len(files)
    for entry in result.entries:
        assert entry.raw_bytes == files[entry.path]
        assert entry.text.encode() == files[entry.path]
        assert (
            entry.blob_sha
            == git(root, "rev-parse", f"{pin}:{entry.path}").decode().strip()
        )
        assert entry.commit_sha == pin
        assert entry.source_ref == f"{entry.path}@{pin}"
        assert entry.text_sha256 == hashlib.sha256(files[entry.path]).hexdigest()
        assert entry.verbatim_verified
        assert entry.policy_id == "fixture-v1"
    assert ModelGitSourceResult.model_validate_json(result.model_dump_json()) == result


@pytest.mark.parametrize(
    ("tenant", "repository", "pin"),
    [("other", "corpus", None), ("lab", "other", None), ("lab", "corpus", "0" * 40)],
)
def test_denied_before_reader_access(
    corpus: tuple[Path, str, dict[str, bytes]],
    tenant: str,
    repository: str,
    pin: str | None,
) -> None:
    class ForbiddenReader:
        def read(self, *args: object, **kwargs: object) -> object:
            pytest.fail("a denied request reached Git")

    node = HandlerContextSourceGit(overlay=overlay(corpus), reader=ForbiddenReader())
    result = node.handle(request(pin or corpus[1], tenant, repository))
    assert result.status.value == "denied"
    assert not result.entries
    assert not result.tree_paths


@pytest.mark.parametrize("paths", [("missing.md",), ("rules.yaml", "missing.md")])
def test_missing_blob_is_unavailable_without_partial_entries(
    corpus: tuple[Path, str, dict[str, bytes]], paths: tuple[str, ...]
) -> None:
    result = HandlerContextSourceGit(overlay=overlay(corpus, paths=paths)).handle(
        request(corpus[1])
    )
    assert result.status.value == "unavailable"
    assert result.entries == ()


def test_missing_commit_is_unavailable(
    corpus: tuple[Path, str, dict[str, bytes]],
) -> None:
    result = HandlerContextSourceGit(
        overlay=overlay(corpus, commit_sha="0" * 40)
    ).handle(request("0" * 40))
    assert result.status.value == "unavailable"
    assert not result.entries


@pytest.mark.parametrize(
    "path",
    [
        "../outside",
        "/etc/passwd",
        ".git/config",
        "a/../b",
        "a//b",
        "./a",
        "-option",
        "a\x00b",
    ],
)
def test_unsafe_paths_fail_validation(
    corpus: tuple[Path, str, dict[str, bytes]], path: str
) -> None:
    with pytest.raises(ValidationError):
        overlay(corpus, paths=[path])


def test_policy_is_strict_and_pins_are_full_hashes(
    corpus: tuple[Path, str, dict[str, bytes]],
) -> None:
    for changes in (
        {"commit_sha": "HEAD"},
        {"unknown": True},
        {"paths": []},
        {"tenant_ids": []},
        {"repository_path": "relative"},
    ):
        with pytest.raises(ValidationError):
            overlay(corpus, **changes)
    source = overlay(corpus).sources[0]
    with pytest.raises(ValidationError, match="duplicate"):
        ModelGitSourceOverlay(sources=(source, source))


def test_overlay_file_is_loaded_and_missing_overlay_fails_closed(
    corpus: tuple[Path, str, dict[str, bytes]],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tests.node_overlay_support import install_node_overlay

    monkeypatch.setenv("ONEX_SKILL_OVERLAY_ROOTS", str(tmp_path / "absent"))
    unconfigured = HandlerContextSourceGit()
    assert unconfigured.handle(request(corpus[1])).status.value == "unavailable"
    install_node_overlay(
        monkeypatch,
        tmp_path,
        "node_context_source_git_effect",
        overlay(corpus).model_dump(mode="json"),
    )
    assert (
        HandlerContextSourceGit().handle(request(corpus[1])).status.value == "resolved"
    )


def test_git_location_environment_cannot_retarget_reader(
    corpus: tuple[Path, str, dict[str, bytes]],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for key in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_COMMON_DIR"):
        monkeypatch.setenv(key, str(tmp_path / "not-the-source"))
    result = HandlerContextSourceGit(overlay=overlay(corpus)).handle(request(corpus[1]))
    assert result.status.value == "resolved"
    assert {entry.path: entry.raw_bytes for entry in result.entries} == corpus[2]


def test_no_age_drop_or_budget_and_source_keys_preserve_parameters(
    corpus: tuple[Path, str, dict[str, bytes]],
) -> None:
    result = HandlerContextSourceGit(
        overlay=overlay(corpus), now=lambda: datetime(2100, 1, 1, tzinfo=UTC)
    ).handle(request(corpus[1]))
    assert len(result.entries) == len(corpus[2])
    assert all(entry.freshness == "stale" for entry in result.entries)
    assert len({e.source_ref for e in result.entries if e.path.startswith("doc?")}) == 2
    assert not {"budget", "pack_hash"} & type(result).model_fields.keys()


@pytest.mark.parametrize("binding", ["typed-runtime", "in-memory-bus"])
@pytest.mark.parametrize(
    ("tenant", "expected"), [("lab", "resolved"), ("other", "denied")]
)
def test_same_context_exchange_across_bindings(
    corpus: tuple[Path, str, dict[str, bytes]], binding: str, tenant: str, expected: str
) -> None:
    from omnibase_core.event_bus.event_bus_inmemory import EventBusInmemory
    from omnibase_infra.runtime.auto_wiring.discovery import (
        discover_contracts_from_paths,
    )
    from omnibase_infra.runtime.auto_wiring.handler_wiring import (
        _make_dispatch_callback,
    )

    manifest = discover_contracts_from_paths([CONTRACT])
    assert not manifest.errors
    route = manifest.contracts[0].handler_routing.handlers[0]
    node = HandlerContextSourceGit(overlay=overlay(corpus), now=lambda: NOW)
    callback = _make_dispatch_callback(node, event_model=route.event_model)
    contract = yaml.safe_load(CONTRACT.read_text())
    command = contract["runtime_dispatch"]["command_topic"]
    terminal = contract["terminal_event"]
    assert contract["event_bus"]["publish_topics"] == [
        "onex.evt.omnimarket.context-source-git-completed.v1"
    ]
    assert terminal == node.terminal_event
    envelope = {
        "payload": request(corpus[1], tenant).model_dump(mode="json"),
        "correlation_id": str(uuid4()),
    }

    async def exchange() -> ModelGitSourceResult:
        if binding == "typed-runtime":
            dispatched = await callback(envelope)
            assert dispatched is not None
            return ModelGitSourceResult.model_validate(dispatched.output_events[0])
        bus = EventBusInmemory()
        await bus.start()
        answers: list[ModelGitSourceResult] = []

        async def receive(message: object) -> None:
            answers.append(ModelGitSourceResult.model_validate_json(message.value))

        async def dispatch(message: object) -> None:
            dispatched = await callback(json.loads(message.value))
            assert dispatched is not None
            await bus.publish(
                terminal,
                key=None,
                value=ModelGitSourceResult.model_validate(dispatched.output_events[0])
                .model_dump_json()
                .encode(),
            )

        await bus.subscribe(terminal, group_id="result", on_message=receive)
        await bus.subscribe(command, group_id="source", on_message=dispatch)
        try:
            await bus.publish(command, key=None, value=json.dumps(envelope).encode())
            assert len(answers) == 1
            return answers[0]
        finally:
            await bus.shutdown()

    result = asyncio.run(exchange())
    assert result.status.value == expected
    assert {e.path: e.raw_bytes for e in result.entries} == (
        corpus[2] if expected == "resolved" else {}
    )
