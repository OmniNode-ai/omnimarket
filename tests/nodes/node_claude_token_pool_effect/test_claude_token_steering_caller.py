# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The lane start asks the usage verdict and passes the answer to the pool only when the overlay says so (OMN-23636).

Three layers, each with its failing control. The caller (``HandlerPoolSteering``): off answers nothing
and runs nothing; on runs the overlay's verdict command, reads ``drain_labels`` from its JSON and counts
the open lanes per label from the ledger. The ssh transport: the steer's arguments reach the real pool
node's ``resolve`` under a real shell, at the draw and in the engine's own shell, and with no steer the
host script is the one it was. The orchestrator: a start the pool refuses at its cap claims nothing and
starts no engine. Every token here is fake, assembled at run time.
"""

from __future__ import annotations

import json
import stat
import sys
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from omnibase_spi.contracts.services import (
    ModelLaneHostRef,
    ModelLaneStartEngineRequest,
)
from pydantic import SecretStr

from omnimarket.inference.local_byok_credential_adapter import LocalByokCredentialStore
from omnimarket.models.engine_login import (
    ModelEngineLoginDraw,
    ModelEngineLoginLaneCount,
    ModelEngineLoginPool,
    ModelEngineLoginSteer,
)
from omnimarket.nodes.node_claude_token_pool_effect.handlers import (
    handler_pool_steering as steering_module,
)
from omnimarket.nodes.node_claude_token_pool_effect.handlers.handler_claude_token_pool import (
    HandlerClaudeTokenPool,
)
from omnimarket.nodes.node_claude_token_pool_effect.handlers.handler_pool_steering import (
    HandlerPoolSteering,
    count_open_lanes,
    lane_start_steer,
)
from omnimarket.nodes.node_claude_token_pool_effect.models.model_claude_token_pool import (
    ModelClaudeTokenPoolRequest,
)
from omnimarket.nodes.node_claude_token_pool_effect.models.model_claude_token_rotation import (
    ModelRotationSteering,
)
from omnimarket.nodes.node_remote_lane_start_effect.adapters.adapter_ssh_transport import (
    ModelHostCommandResult,
    SshLaneHostTransport,
    SubprocessHostCommandExecutor,
)
from omnimarket.nodes.node_remote_lane_start_orchestrator.models.model_remote_lane_start import (
    EXIT_CODES,
    EnumRemoteLaneStartOutcome,
)
from omnimarket.nodes.node_remote_lane_start_orchestrator.tests.lane_start_fixtures import (
    Rig,
    request,
)

pytestmark = pytest.mark.unit

T0 = datetime(2026, 11, 10, 21, 0, tzinfo=UTC)
POOL_MODULE = "omnimarket.nodes.node_claude_token_pool_effect"
LOCAL = ModelLaneHostRef(name="h-test", local=True)
ON = ModelRotationSteering(
    enabled=True,
    verdict_argv=("verdict-command", "--json"),
    verdict_timeout_seconds=7,
    open_claim_max_age_seconds=3600,
)
OFF = ModelRotationSteering(enabled=False)


def fake_token(seed: str) -> str:
    return "sk-ant-" + "oat01-" + (seed * 64)[:96]


def stamp(minutes_ago: int) -> str:
    return (T0 - timedelta(minutes=minutes_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")


def row(kind: str, lane: str, minutes_ago: int, *cells: str) -> str:
    return " | ".join([stamp(minutes_ago), kind, f"lane={lane}", *cells, "text"])


class Ledger:
    def __init__(self, *rows: str) -> None:
        self.text = "\n".join(rows) + "\n"
        self.reads = 0

    def read_text(self) -> str:
        self.reads += 1
        return self.text


class Verdict:
    """The verdict command, recorded: answers the JSON it was given with the exit it was given."""

    def __init__(self, out: str, code: int = 0) -> None:
        self.out, self.code = out, code
        self.calls: list[tuple[tuple[str, ...], float]] = []

    def __call__(self, argv: Sequence[str], timeout: float) -> tuple[int, str]:
        self.calls.append((tuple(argv), timeout))
        return self.code, self.out


VERDICT_RESULT = json.dumps(
    {
        "labels": [],
        "drain_labels": ["acct-a"],
        "ok": 1,
        "drain": 1,
        "limited": 0,
        "unknown": 0,
        "correlation_id": "6e375228-96c7-400a-a728-e8950a8144e9",
    }
)


# ---- the caller ---------------------------------------------------------------------------------


@pytest.mark.parametrize("steering", [None, OFF])
def test_switched_off_the_caller_answers_nothing_and_asks_nobody(
    steering: ModelRotationSteering | None,
) -> None:
    verdict, ledger = Verdict(VERDICT_RESULT), Ledger()
    caller = HandlerPoolSteering(steering=steering, ledger=ledger, run=verdict)
    assert caller.handle() is None
    assert verdict.calls == []
    assert ledger.reads == 0


def test_switched_on_the_caller_asks_the_verdict_and_passes_its_drain_labels() -> None:
    verdict = Verdict(VERDICT_RESULT)
    ledger = Ledger(
        row("CLAIM", "lane-1", 30, "login=acct-a"),
        row("CLAIM", "lane-2", 20, "login=acct-a"),
        row("CLAIM", "lane-3", 10, "login=acct-b"),
    )
    steer = HandlerPoolSteering(
        steering=ON, ledger=ledger, run=verdict, now=lambda: T0
    ).handle()
    assert verdict.calls == [(("verdict-command", "--json"), 7)]
    assert steer == ModelEngineLoginSteer(
        drain_labels=("acct-a",),
        lane_counts=(
            ModelEngineLoginLaneCount(label="acct-a", lanes=2),
            ModelEngineLoginLaneCount(label="acct-b", lanes=1),
        ),
    )
    assert steer is not None
    assert steer.resolve_arguments() == (
        "--drain-labels",
        "acct-a",
        "--lanes",
        "acct-a=2,acct-b=1",
    )


@pytest.mark.parametrize(
    ("out", "code", "why"),
    [
        (VERDICT_RESULT, 3, "ended 3"),
        ("not json", 0, "reading its result"),
        (json.dumps({"drained": ["acct-a"]}), 0, "reading its result"),
        (json.dumps({"drain_labels": [{"label": "acct-a"}]}), 0, "reading its result"),
        (json.dumps({"drain_labels": ["Acct A; rm"]}), 0, "not pool labels"),
    ],
)
def test_a_verdict_that_cannot_be_had_steers_nothing_and_says_why(
    out: str, code: int, why: str
) -> None:
    steer = HandlerPoolSteering(
        steering=ON, ledger=Ledger(), run=Verdict(out, code), now=lambda: T0
    ).handle()
    assert steer is not None
    assert steer.drain_labels == ()
    assert why in steer.detail


def test_a_verdict_command_that_does_not_exist_answers_a_failure() -> None:
    code, _ = steering_module.run_verdict_command(
        ["/no/such/verdict-command-omn23636"], 5
    )
    assert code != 0


def test_open_lanes_are_counted_per_label_from_the_open_claims() -> None:
    text = "\n".join(
        [
            row("CLAIM", "ended", 50, "login=acct-a"),
            row("TERMINAL", "ended", 40),
            row("CLAIM", "running-1", 30, "host=h1", "login=acct-a"),
            row("CLAIM", "running-2", 20, "login=acct-b"),
            row("CLAIM", "no-pool", 20, "login=none"),
            row("CLAIM", "by-hand", 20),
            row("CLAIM", "dead", 600, "login=acct-a"),
            row("CLAIM", "reclaimed", 50, "login=acct-a"),
            row("TERMINAL", "reclaimed", 45),
            row("CLAIM", "reclaimed", 5, "login=acct-b"),
            row("STATUS", "running-1", 1, "login=acct-c"),
            "not a row",
        ]
    )
    counts = count_open_lanes(text, now=T0, max_age_seconds=3600)
    assert {c.label: c.lanes for c in counts} == {"acct-a": 1, "acct-b": 2}
    # The failing control of the age bound: widen it and the dead lane's CLAIM counts again.
    wide = count_open_lanes(text, now=T0, max_age_seconds=86400)
    assert {c.label: c.lanes for c in wide}["acct-a"] == 2


def test_the_lane_start_steer_is_off_without_an_overlay_and_reads_the_switch_from_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import yaml

    node = Path(steering_module.__file__).resolve().parents[1]
    document = yaml.safe_load((node / "overlay.example.yaml").read_text("utf-8"))
    root = tmp_path / "roots"
    target = root / "node_claude_token_pool_effect" / "overlay.yaml"
    target.parent.mkdir(parents=True)
    ledger = Ledger(row("CLAIM", "lane-1", 5, "login=acct-a"))
    steer = lane_start_steer(ledger)

    monkeypatch.setenv("ONEX_SKILL_OVERLAY_ROOTS", str(tmp_path / "empty"))
    assert steer() is None

    monkeypatch.setenv("ONEX_SKILL_OVERLAY_ROOTS", str(root))
    document["steering"] = {"enabled": False}
    target.write_text(yaml.safe_dump(document), encoding="utf-8")
    assert steer() is None

    printer = tmp_path / "verdict.py"
    printer.write_text(f"print({VERDICT_RESULT!r})\n", encoding="utf-8")
    document["steering"] = {
        "enabled": True,
        "verdict_argv": [sys.executable, str(printer)],
        "verdict_timeout_seconds": 30,
        "open_claim_max_age_seconds": 10**9,
    }
    target.write_text(yaml.safe_dump(document), encoding="utf-8")
    answered = steer()
    assert answered is not None
    assert answered.drain_labels == ("acct-a",)
    assert answered.lane_counts == (ModelEngineLoginLaneCount(label="acct-a", lanes=1),)


# ---- the ssh transport, under a real shell, over the real pool node -----------------------------


class Spy:
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
    """A temporary registry root whose interpreter runs the real pool node, with the cap it is given."""

    def __init__(self, tmp: Path, cap: int | None = None) -> None:
        self.root = tmp / "registry"
        store, status = tmp / "store" / "evidence.sqlite", tmp / "cache" / "pool.json"
        launcher = tmp / "launcher.py"
        launcher.write_text(
            "import sys\n"
            "from pathlib import Path\n"
            "from omnimarket.inference.local_byok_credential_adapter import LocalByokCredentialStore\n"
            "from omnimarket.nodes.node_claude_token_pool_effect import runtime_claude_token_pool as cli\n"
            "from omnimarket.nodes.node_claude_token_pool_effect.handlers.handler_claude_token_pool import HandlerClaudeTokenPool\n"
            f"handler = HandlerClaudeTokenPool(store=LocalByokCredentialStore(db_path=Path({str(store)!r})), "
            f"status_path=Path({str(status)!r}), host='h-test', lane_cap={cap!r})\n"
            "raise SystemExit(cli.main(sys.argv[3:], handler=handler))\n",
            encoding="utf-8",
        )
        python = self.root / ".test-venv" / "bin" / "python"
        python.parent.mkdir(parents=True)
        python.write_text(
            f'#!/bin/sh\nexec "{sys.executable}" "{launcher}" "$@"\n', encoding="utf-8"
        )
        python.chmod(python.stat().st_mode | stat.S_IXUSR)
        pool = HandlerClaudeTokenPool(
            store=LocalByokCredentialStore(db_path=store), status_path=status
        )
        self.tokens = {label: fake_token(label[-1]) for label in ("acct-a", "acct-b")}
        for label, token in self.tokens.items():
            pool.handle(
                ModelClaudeTokenPoolRequest(
                    operation="add", label=label, setup_token_output=SecretStr(token)
                )
            )

    def transport(
        self, runner: Spy, run_root: Path, steer: ModelEngineLoginSteer | None
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
            login_steer=lambda: steer,
            runner=runner,
        )


def test_the_draw_skips_the_drained_label_the_steer_names(tmp_path: Path) -> None:
    host, spy = Host(tmp_path), Spy()
    steer = ModelEngineLoginSteer(drain_labels=("acct-a",))
    drawn = host.transport(spy, tmp_path / "runs", steer).draw_login(LOCAL)
    assert (drawn.state, drawn.label) == ("drawn", "acct-b")
    assert all(token not in "".join(spy.seen) for token in host.tokens.values())


def test_the_draw_with_the_switch_off_is_the_first_label_and_the_script_it_was(
    tmp_path: Path,
) -> None:
    # The failing control of the test above, and the proof that off changes no host command.
    host, spy = Host(tmp_path), Spy()
    drawn = host.transport(spy, tmp_path / "runs", None).draw_login(LOCAL)
    assert drawn.label == "acct-a"
    assert " resolve 2>&1 >/dev/null" in spy.seen[0]
    assert "--drain-labels" not in spy.seen[0]
    assert "--lanes" not in spy.seen[0]


def test_the_draw_at_the_cap_answers_refused_with_the_pools_code(
    tmp_path: Path,
) -> None:
    host = Host(tmp_path, cap=1)
    steer = ModelEngineLoginSteer(
        lane_counts=(
            ModelEngineLoginLaneCount(label="acct-a", lanes=1),
            ModelEngineLoginLaneCount(label="acct-b", lanes=3),
        )
    )
    drawn = host.transport(Spy(), tmp_path / "runs", steer).draw_login(LOCAL)
    assert drawn == ModelEngineLoginDraw(
        state="refused", refusal="label_lane_cap", detail=drawn.detail
    )
    assert "oat01" not in drawn.detail


def test_the_engines_own_resolve_carries_the_arguments_of_the_draw(
    tmp_path: Path,
) -> None:
    host, spy = Host(tmp_path), Spy()
    steer = ModelEngineLoginSteer(drain_labels=("acct-a",))
    transport = host.transport(spy, tmp_path / "runs", steer)
    transport.draw_login(LOCAL)
    workdir = tmp_path / "work"
    transport.start_engine_detached(
        ModelLaneStartEngineRequest(
            host=LOCAL,
            lane="steer-lane",
            run_id="run-steer-1",
            workdir=str(workdir),
            engine_argv=("true",),
            environment=(),
            timeout_seconds=60,
        )
    )
    assert "resolve --drain-labels acct-a --lanes " in spy.seen[-1]
    assert "2>/dev/null); then export CLAUDE_CODE_OAUTH_TOKEN" in spy.seen[-1]


# ---- the orchestrator ---------------------------------------------------------------------------


class RefusingDraw:
    def __init__(self, calls: list[str]) -> None:
        self.calls = calls

    def draw_login(self, host: ModelLaneHostRef) -> ModelEngineLoginDraw:
        self.calls.append(f"host:draw_login host={host.name}")
        return ModelEngineLoginDraw(state="refused", refusal="label_lane_cap")


def test_a_start_the_pool_refuses_claims_nothing_and_starts_no_engine(
    tmp_path: Path,
) -> None:
    rig = Rig(tmp_path, login_labels=("acct-a",))
    rig.orchestrator._login = RefusingDraw(rig.calls)
    result = rig.orchestrator.run(request())
    assert result.outcome is EnumRemoteLaneStartOutcome.LOGIN_REFUSED
    assert result.exit_code == EXIT_CODES[EnumRemoteLaneStartOutcome.LOGIN_REFUSED]
    assert result.exit_code == 75
    assert any(
        line.startswith("REMOTE-LANE-LOGIN-REFUSED")
        and "refusal=label_lane_cap" in line
        for line in rig.printed
    )
    assert not rig.ledger_path.exists() or rig.ledger_rows("CLAIM") == []
    assert not any("start_engine" in call for call in rig.calls)


def test_the_same_start_with_a_drawn_login_claims_and_starts(tmp_path: Path) -> None:
    # The failing control of the test above.
    rig = Rig(tmp_path, login_labels=("acct-a",))
    result = rig.orchestrator.run(request())
    assert result.outcome is not EnumRemoteLaneStartOutcome.LOGIN_REFUSED
    assert len(rig.ledger_rows("CLAIM")) == 1
