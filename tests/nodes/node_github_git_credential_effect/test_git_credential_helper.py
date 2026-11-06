# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The git credential helper speaks git's protocol and writes nothing to disk (OMN-22613).

``get`` for the overlay's host with a path prints the username and the token;
another host prints nothing so git falls through; ``store`` and ``erase`` print
nothing. A full round trip runs the real handler behind a bus fake: the helper
sends only a public key, the node seals, the helper opens.
"""

from __future__ import annotations

import contextlib
import json
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from omnimarket.handlers.scm.scm_git_credential import (
    BusGitCredentialAsker,
    GitCredentialRefusedError,
    answer_git_credential,
    git_credential_command,
    node_topics,
)
from omnimarket.nodes.node_github_git_credential_effect.handlers import (
    HandlerGithubGitCredentialEffect,
)
from omnimarket.nodes.node_github_git_credential_effect.handlers.handler_github_git_credential import (
    packaged_policy,
)
from omnimarket.nodes.node_github_git_credential_effect.models import (
    ModelGitCredentialRequest,
)

from .support import FAKE_TOKEN, FakeMinter

pytestmark = pytest.mark.unit

_GET = "protocol=https\nhost=github.com\npath=OmniNode-ai/omnimarket.git\n\n"


class _Asker:
    def __init__(self) -> None:
        self.asked: list[str] = []

    async def ask(self, repository: str) -> str:
        self.asked.append(repository)
        return FAKE_TOKEN


async def test_get_prints_the_username_and_token_for_the_repository() -> None:
    asker = _Asker()
    out = await answer_git_credential("get", _GET, asker, git_host="github.com")
    assert out == f"username=x-access-token\npassword={FAKE_TOKEN}\n"
    assert asker.asked == ["OmniNode-ai/omnimarket"]


@pytest.mark.parametrize(
    ("action", "text"),
    [
        ("get", "protocol=https\nhost=gitlab.com\npath=a/b.git\n\n"),
        ("get", "protocol=http\nhost=github.com\npath=a/b.git\n\n"),
        ("store", _GET + "username=x\npassword=y\n"),
        ("erase", _GET),
    ],
)
async def test_other_hosts_and_store_and_erase_print_nothing(
    action: str, text: str
) -> None:
    asker = _Asker()
    assert await answer_git_credential(action, text, asker, git_host="github.com") == ""
    assert asker.asked == []


async def test_get_without_a_path_names_use_http_path() -> None:
    with pytest.raises(GitCredentialRefusedError, match="useHttpPath"):
        await answer_git_credential(
            "get",
            "protocol=https\nhost=github.com\n\n",
            _Asker(),
            git_host="github.com",
        )


def _files(root: Path) -> set[Path]:
    return set(root.rglob("*"))


@pytest.mark.parametrize(
    ("action", "text"),
    [
        ("store", _GET + "username=x\npassword=y\n"),
        ("erase", _GET),
        ("get", "protocol=https\nhost=gitlab.com\npath=a/b.git\n\n"),
    ],
)
def test_the_command_writes_no_file_under_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, action: str, text: str
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("ONEX_SKILL_OVERLAY_ROOTS", raising=False)
    before = _files(tmp_path)
    result = CliRunner().invoke(
        git_credential_command,
        [action, "--kafka-bootstrap", "127.0.0.1:1"],
        input=text,
    )
    assert result.exit_code == 0, result.output
    assert result.output == ""
    assert _files(tmp_path) == before


class _NodeBehindABus:
    """A bus fake that answers the command topic with the real handler."""

    def __init__(self, handler: HandlerGithubGitCredentialEffect) -> None:
        self._handler = handler
        self._subs: dict[str, Callable[[Any], Awaitable[None]]] = {}
        self.published: list[bytes] = []

    async def publish(
        self, topic: str, key: bytes | None, value: bytes, headers: Any = None
    ) -> object:
        self.published.append(value)
        topics = node_topics()
        assert topic == topics.command
        envelope = json.loads(value)
        request = ModelGitCredentialRequest.model_validate(envelope["payload"])
        result = await self._handler.handle(request)
        out = topics.failure if result.outcome == "failed" else topics.success
        assert out is not None

        class _Message:
            value = json.dumps(
                {
                    "correlation_id": envelope["correlation_id"],
                    "payload": result.model_dump(mode="json"),
                }
            ).encode()

        self.published.append(_Message.value)
        await self._subs[out](_Message())
        return None

    async def subscribe(
        self, *args: Any, **kwargs: Any
    ) -> Callable[[], Awaitable[None]]:
        raise AssertionError("the helper reads its terminal with no group")

    async def subscribe_ungrouped(
        self, topic: str, on_message: Callable[[Any], Awaitable[None]]
    ) -> Callable[[], Awaitable[None]]:
        self._subs[topic] = on_message

        async def unsubscribe() -> None:
            self._subs.pop(topic, None)

        return unsubscribe


async def test_a_round_trip_opens_the_token_and_the_bus_never_carries_it() -> None:
    bus = _NodeBehindABus(
        HandlerGithubGitCredentialEffect(FakeMinter(), policy=packaged_policy())
    )

    @contextlib.asynccontextmanager
    async def opened() -> AsyncIterator[_NodeBehindABus]:
        yield bus

    asker = BusGitCredentialAsker(opened, timeout_s=5, lane="lane-x", host="h201")  # type: ignore[arg-type]
    out = await answer_git_credential("get", _GET, asker, git_host="github.com")
    assert out.endswith(f"password={FAKE_TOKEN}\n")
    assert len(bus.published) == 2
    assert all(FAKE_TOKEN.encode() not in value for value in bus.published)


async def test_a_refusal_from_the_node_is_raised_with_its_reason() -> None:
    bus = _NodeBehindABus(
        HandlerGithubGitCredentialEffect(FakeMinter(), policy=packaged_policy())
    )

    @contextlib.asynccontextmanager
    async def opened() -> AsyncIterator[_NodeBehindABus]:
        yield bus

    asker = BusGitCredentialAsker(opened, timeout_s=5, lane=None, host=None)  # type: ignore[arg-type]
    with pytest.raises(GitCredentialRefusedError, match="owner_not_allowed"):
        await answer_git_credential(
            "get",
            "protocol=https\nhost=github.com\npath=someone-else/repo.git\n\n",
            asker,
            git_host="github.com",
        )
