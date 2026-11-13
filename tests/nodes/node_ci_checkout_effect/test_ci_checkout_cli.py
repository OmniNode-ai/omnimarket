# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""``onex ci checkout`` end to end with GitHub blocked: the node's real handler answers from a real bare
mirror, the CLI materializes the tree from the URL in the reply, and no step names GitHub.

The bus hop is replaced by the handler call itself (the golden chain test covers the bus hop); everything
else is the shipped code: the request model, the handler over real git, the result model and materialize.
"""

from __future__ import annotations

import contextlib
import json
import os
import subprocess
import uuid
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import yaml
from click.testing import CliRunner
from omnibase_core.validators.no_unguarded_git_subprocess import (
    scrub_git_location_env,
)

from omnimarket.delegated_test_loop.lane_bus import LabRunBusError
from omnimarket.handlers.scm.handler_scm_dispatch import ScmBusCaller
from omnimarket.models.scm import ModelScmBusReceipt, ModelScmTerminal
from omnimarket.nodes.node_ci_checkout_effect.handlers import (
    adapter_ci_checkout_cli as cli,
)
from omnimarket.nodes.node_ci_checkout_effect.handlers.handler_ci_checkout import (
    HandlerCiCheckout,
    load_overlay,
)
from omnimarket.nodes.node_ci_checkout_effect.models.model_ci_checkout import (
    ModelCiCheckoutRequest,
)
from tests.nodes.node_ci_checkout_effect.planted import deployment_document

pytestmark = pytest.mark.unit

HOST = "runner-host"


def _git(*argv: str, cwd: Path) -> str:
    return subprocess.run(
        ["git", *argv],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
        env=scrub_git_location_env(os.environ),
    ).stdout.strip()


@pytest.fixture
def mirror(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    """A bare mirror of one repo holding one commit, the overlay naming it, and a GitHub that is dead."""
    work = tmp_path / "work"
    work.mkdir()
    _git("init", "-q", cwd=work)
    (work / "tracked.txt").write_text("from the mirror\n")
    _git("add", "tracked.txt", cwd=work)
    _git(
        "-c",
        "user.name=t",
        "-c",
        "user.email=t@example.invalid",
        "commit",
        "-q",
        "-m",
        "m",
        cwd=work,
    )
    sha = _git("rev-parse", "HEAD", cwd=work)
    mirrors = tmp_path / "mirrors"
    mirrors.mkdir()
    _git(
        "clone",
        "-q",
        "--bare",
        str(work),
        str(mirrors / "omnimarket.git"),
        cwd=tmp_path,
    )
    overlay = tmp_path / "overlay.yaml"
    overlay.write_text(yaml.safe_dump(deployment_document(mirrors, host=HOST)))
    handler = HandlerCiCheckout(overlay=load_overlay(overlay, host_name=HOST))

    @contextlib.asynccontextmanager
    async def fake_bus(_opts: object) -> AsyncIterator[object]:
        yield object()

    async def answer(
        self: ScmBusCaller,
        *,
        topics,
        request: ModelCiCheckoutRequest,
        correlation_id,
        timeout_s,
    ) -> ModelScmTerminal:
        result = handler.handle(request)
        return ModelScmTerminal(
            receipt=ModelScmBusReceipt(
                correlation_id=correlation_id,
                command_topic=topics.command,
                command_envelope_id=uuid.uuid4(),
                terminal_topic=topics.success,
            ),
            failed=False,
            payload=json.loads(result.model_dump_json()),
        )

    monkeypatch.setattr(cli, "_open_bus", fake_bus)
    # The caller's constructor only checks the bus can join no consumer group; the hop itself is replaced below.
    monkeypatch.setattr(ScmBusCaller, "__init__", lambda *_args: None)
    monkeypatch.setattr(ScmBusCaller, "call_topics", answer)
    for name in ("HTTPS_PROXY", "HTTP_PROXY", "ALL_PROXY"):
        monkeypatch.setenv(name, "http://127.0.0.1:9")
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "url.http://127.0.0.1:9/.insteadOf")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "https://github.com/")
    return sha


def _run(sha: str, dest: Path, host: str = HOST) -> tuple[int, dict[str, object]]:
    outcome = CliRunner().invoke(
        cli.ci_group,
        [
            "checkout",
            "--repo",
            "OmniNode-ai/omnimarket",
            "--sha",
            sha,
            "--dest",
            str(dest),
            "--lane",
            "test-lane",
            "--host",
            host,
            "--bus",
            "inmemory",
            "--timeout-s",
            "20",
        ],
    )
    assert outcome.output.strip(), repr(outcome.exception)
    return outcome.exit_code, json.loads(outcome.output.strip().splitlines()[-1])


def test_checkout_lands_the_tree_with_github_blocked(
    mirror: str, tmp_path: Path
) -> None:
    dest = tmp_path / "workspace" / "repo"
    code, answer = _run(mirror, dest)
    assert code == 0, answer
    assert answer["status"] == "served"
    assert answer["upstream_fetches"] == 0
    assert (dest / "tracked.txt").read_text() == "from the mirror\n"
    assert _git("rev-parse", "HEAD", cwd=dest) == mirror
    assert _git("remote", cwd=dest) == ""


def test_failing_control_a_sha_the_mirror_lacks_checks_nothing_out(
    mirror: str, tmp_path: Path
) -> None:
    dest = tmp_path / "workspace" / "repo"
    code, answer = _run("c" * 40, dest)
    assert code == 1
    assert answer["status"] == "not_found"
    assert not dest.exists()


def test_a_request_addressed_to_another_host_is_refused(
    mirror: str, tmp_path: Path
) -> None:
    code, answer = _run(mirror, tmp_path / "x", host="some-other-host")
    assert code == 1
    assert answer["status"] == "refused"


def test_no_terminal_exits_75_and_a_closed_bus_exits_69(
    mirror: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def silent(self: ScmBusCaller, **_kw: object) -> ModelScmTerminal:
        raise TimeoutError

    monkeypatch.setattr(ScmBusCaller, "call_topics", silent)
    code, answer = _run(mirror, tmp_path / "a")
    assert (code, answer["status"]) == (75, "no_terminal")

    async def closed(self: ScmBusCaller, **_kw: object) -> ModelScmTerminal:
        raise LabRunBusError("no broker")

    monkeypatch.setattr(ScmBusCaller, "call_topics", closed)
    code, answer = _run(mirror, tmp_path / "b")
    assert (code, answer["status"]) == (69, "bus_unavailable")
