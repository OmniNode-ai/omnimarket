# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The lane start draws its engine login through the host's token pool node, in draw order, skipping limited labels (OMN-23425).

On h051 on 2026-11-10 the start nodes started a real lane and the engine answered a weekly limit,
while lanes the runner started on the same host drew a pool account that was not limited: the
runner exports the pool node's ``resolve`` as the engine's login and the start nodes did not.
These tests run the real pool node (its ``resolve`` verb over a temporary secret store) under a
real shell through the ssh transport, so a limited label first in the pool is the case that fails.
Every token here is fake, assembled at run time, and none may appear in an argv, an output or a row.
"""

from __future__ import annotations

import hashlib
import stat
import sys
import time
from collections.abc import Sequence
from pathlib import Path

import pytest
from omnibase_spi.contracts.services import (
    ModelLaneHostRef,
    ModelLaneReadEngineExitRequest,
    ModelLaneStartEngineRequest,
)
from pydantic import SecretStr

from omnimarket.inference.local_byok_credential_adapter import LocalByokCredentialStore
from omnimarket.models.engine_login import (
    ModelEngineLoginPool,
)
from omnimarket.nodes.node_claude_token_pool_effect.handlers.handler_claude_token_pool import (
    HandlerClaudeTokenPool,
)
from omnimarket.nodes.node_claude_token_pool_effect.models.model_claude_token_pool import (
    ModelClaudeTokenPoolRequest,
)
from omnimarket.nodes.node_remote_lane_start_effect.adapters.adapter_scripted_transport import (
    ModelScriptedTransportScript,
)
from omnimarket.nodes.node_remote_lane_start_effect.adapters.adapter_ssh_transport import (
    ModelHostCommandResult,
    SshLaneHostTransport,
    SubprocessHostCommandExecutor,
)
from omnimarket.nodes.node_remote_lane_start_orchestrator.models.model_remote_lane_start import (
    ModelRemoteLaneStartSettings,
)
from omnimarket.nodes.node_remote_lane_start_orchestrator.tests.lane_start_fixtures import (
    Rig,
    cells,
    request,
    settings,
    stream_result,
)

pytestmark = pytest.mark.unit

POOL_MODULE = "omnimarket.nodes.node_claude_token_pool_effect"
LOCAL = ModelLaneHostRef(name="h-test", local=True)
DAY_SECONDS = 86400.0


def fake_token(seed: str) -> str:
    return "sk-ant-" + "oat01-" + (seed * 64)[:96]


class SpyHostCommands:
    """The production executor, recording every argv and every answer so a leak can be searched for."""

    def __init__(self) -> None:
        self.seen: list[str] = []

    def run(
        self, argv: Sequence[str], timeout_seconds: float
    ) -> ModelHostCommandResult:
        done = SubprocessHostCommandExecutor().run(argv, timeout_seconds)
        self.seen.append(" ".join(argv) + "\n" + done.stdout + "\n" + done.stderr)
        return ModelHostCommandResult(
            exit_code=done.exit_code, stdout=done.stdout, stderr=done.stderr
        )


class Host:
    """A temporary registry root whose dispatch interpreter runs the real pool node over a temporary store."""

    def __init__(self, tmp: Path) -> None:
        self.root = tmp / "registry"
        self.store_db = tmp / "store" / "evidence.sqlite"
        self.status = tmp / "cache" / "claude-token-pool.json"
        self.tokens: dict[str, str] = {}
        launcher = tmp / "launcher.py"
        launcher.write_text(
            "import sys\n"
            "from pathlib import Path\n"
            "from omnimarket.inference.local_byok_credential_adapter import LocalByokCredentialStore\n"
            "from omnimarket.nodes.node_claude_token_pool_effect import runtime_claude_token_pool as cli\n"
            "from omnimarket.nodes.node_claude_token_pool_effect.handlers.handler_claude_token_pool import HandlerClaudeTokenPool\n"
            f"handler = HandlerClaudeTokenPool(store=LocalByokCredentialStore(db_path=Path({str(self.store_db)!r})), status_path=Path({str(self.status)!r}), host='h-test')\n"
            "raise SystemExit(cli.main(sys.argv[3:], handler=handler))\n",
            encoding="utf-8",
        )
        python = self.root / ".test-venv" / "bin" / "python"
        python.parent.mkdir(parents=True)
        python.write_text(
            f'#!/bin/sh\nexec "{sys.executable}" "{launcher}" "$@"\n', encoding="utf-8"
        )
        python.chmod(python.stat().st_mode | stat.S_IXUSR)
        self.pool = HandlerClaudeTokenPool(
            store=LocalByokCredentialStore(db_path=self.store_db),
            status_path=self.status,
            host="h-test",
        )

    def add(self, label: str, seed: str) -> None:
        self.tokens[label] = fake_token(seed)
        self.pool.handle(
            ModelClaudeTokenPoolRequest(
                operation="add",
                label=label,
                setup_token_output=self._wrap(self.tokens[label]),
            )
        )

    @staticmethod
    def _wrap(token: str) -> SecretStr:
        return SecretStr(token)

    def limit(self, label: str) -> None:
        self.pool.handle(
            ModelClaudeTokenPoolRequest(
                operation="mark_limited", label=label, seconds=DAY_SECONDS
            )
        )

    def transport(
        self, runner: SpyHostCommands, run_root: Path
    ) -> SshLaneHostTransport:
        return SshLaneHostTransport(
            ssh_argv=["ssh"],
            host_run_root=str(run_root),
            call_timeout_seconds=60,
            detach_argv=["env"],
            login_pool=ModelEngineLoginPool(
                module=POOL_MODULE,
                python_relative=".test-venv/bin/python",
                roots=(str(self.root),),
                draw_timeout_seconds=60,
            ),
            runner=runner,
        )


@pytest.fixture
def host(tmp_path: Path) -> Host:
    return Host(tmp_path)


def three_accounts(host: Host) -> None:
    host.add("acct-a", "a")
    host.add("acct-b", "b")
    host.add("acct-c", "c")


def test_the_login_draw_skips_a_limited_label_first_in_the_pool(
    host: Host, tmp_path: Path
) -> None:
    three_accounts(host)
    host.limit("acct-a")
    runner = SpyHostCommands()
    drawn = host.transport(runner, tmp_path / "runs").draw_login(LOCAL)
    assert drawn.state == "drawn"
    assert drawn.label == "acct-b"


def test_the_login_draw_controls_take_the_first_label_when_none_is_limited(
    host: Host, tmp_path: Path
) -> None:
    # The failing control of the test above: with nothing limited the same pool draws its first label,
    # so the skip is the pool node's doing and the assertion above can fail.
    three_accounts(host)
    drawn = host.transport(SpyHostCommands(), tmp_path / "runs").draw_login(LOCAL)
    assert drawn.label == "acct-a"


def test_the_login_draw_answers_none_when_every_label_is_limited(
    host: Host, tmp_path: Path
) -> None:
    three_accounts(host)
    for label in ("acct-a", "acct-b", "acct-c"):
        host.limit(label)
    drawn = host.transport(SpyHostCommands(), tmp_path / "runs").draw_login(LOCAL)
    assert (drawn.state, drawn.label) == ("none", None)


def test_the_login_draw_answers_none_with_no_pool_interpreter(tmp_path: Path) -> None:
    transport = SshLaneHostTransport(
        ssh_argv=["ssh"],
        host_run_root=str(tmp_path / "runs"),
        call_timeout_seconds=30,
        detach_argv=["env"],
        login_pool=ModelEngineLoginPool(
            module=POOL_MODULE,
            python_relative=".test-venv/bin/python",
            roots=(str(tmp_path / "no-such-registry"),),
            draw_timeout_seconds=30,
        ),
    )
    drawn = transport.draw_login(LOCAL)
    assert drawn.state == "none"


def test_the_engine_runs_on_the_drawn_token_and_no_token_leaves_the_host(
    host: Host, tmp_path: Path
) -> None:
    three_accounts(host)
    host.limit("acct-a")
    runner = SpyHostCommands()
    run_root = tmp_path / "runs"
    transport = host.transport(runner, run_root)
    workdir = tmp_path / "work"
    workdir.mkdir()
    out = tmp_path / "engine-saw.txt"
    probe = (
        "import hashlib, os, pathlib; t = os.environ.get('CLAUDE_CODE_OAUTH_TOKEN', ''); "
        f"pathlib.Path({str(out)!r}).write_text(hashlib.sha256(t.encode()).hexdigest()[:8])"
    )
    started = transport.start_engine_detached(
        ModelLaneStartEngineRequest(
            host=LOCAL,
            lane="lane-pool",
            run_id="run-pool-1",
            workdir=str(workdir),
            engine_argv=(sys.executable, "-c", probe),
            timeout_seconds=60,
        )
    )
    assert started.state == "started"
    for _ in range(100):
        ended = transport.read_engine_exit(
            ModelLaneReadEngineExitRequest(host=LOCAL, run_id="run-pool-1")
        )
        if not ended.running:
            break
        time.sleep(0.1)
    assert not ended.running
    assert ended.exit_code == 0
    # The engine's login is the second account's token (the first is limited), by fingerprint.
    expect = hashlib.sha256(host.tokens["acct-b"].encode()).hexdigest()[:8]
    assert out.read_text(encoding="utf-8") == expect
    # No token value in any argv or answer the transport saw.
    everything = "\n".join(runner.seen)
    assert all(token not in everything for token in host.tokens.values())
    assert "oat01" not in everything
    log = (run_root / "run-pool-1" / "engine.log").read_text(encoding="utf-8")
    assert "oat01" not in log


def test_the_claim_carries_the_drawn_login_label_and_the_draw_precedes_it(
    tmp_path: Path,
) -> None:
    rig = Rig(
        tmp_path,
        ModelScriptedTransportScript(log_lines=(stream_result("done"),)),
        login_labels=("acct-b",),
    )
    rig.orchestrator.run(request())
    claim = rig.ledger_rows("CLAIM")[0]
    assert cells(claim)["login"] == "acct-b"
    order = [
        c for c in rig.calls if c.startswith(("host:draw_login", "ledger:append CLAIM"))
    ]
    assert order[0].startswith("host:draw_login")
    assert order[1] == "ledger:append CLAIM"


def test_the_claim_says_login_none_when_the_pool_draws_nothing(tmp_path: Path) -> None:
    rig = Rig(tmp_path, login_labels=(None,))
    rig.orchestrator.run(request())
    assert cells(rig.ledger_rows("CLAIM")[0])["login"] == "none"


def test_the_claim_has_no_login_cell_when_no_draw_is_wired(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    rig.orchestrator.run(request())
    assert "login" not in cells(rig.ledger_rows("CLAIM")[0])


@pytest.mark.parametrize(
    "key",
    ["login_pool_module", "login_pool_python_relative", "login_draw_timeout_seconds"],
)
def test_a_missing_login_pool_key_refuses_naming_it(key: str) -> None:
    section = settings().model_dump(mode="json")
    del section[key]
    with pytest.raises(KeyError, match=f"remote_lane_start.{key}"):
        ModelRemoteLaneStartSettings.from_overlay({"remote_lane_start": section})
