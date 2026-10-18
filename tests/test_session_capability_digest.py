# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The session-start capability digest prints inside a registry root and nowhere else."""

import io
import json
import os
import socket
from pathlib import Path

import pytest
import yaml

from omnimarket.nodes.node_session_capability_digest_effect.handlers import (
    HandlerSessionCapabilityDigest,
)
from omnimarket.nodes.node_session_capability_digest_effect.handlers import (
    handler_session_capability_digest_cli as cli,
)
from omnimarket.nodes.node_session_capability_digest_effect.models import (
    EnumSessionCapabilityDigestOutcome,
    ModelSessionCapabilityDigestRequest,
    ModelSessionCapabilityDigestResult,
)

pytestmark = pytest.mark.unit

DIGEST = "CAPABILITIES generated_at=2026-10-17T22:00:00Z\nhosts: h200 up, h201 up\n"
NOW = 1_800_000_000.0
NODE = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "omnimarket"
    / "nodes"
    / "node_session_capability_digest_effect"
)


def write_digest(state: Path, age_minutes: int) -> Path:
    capability = state / "capability"
    capability.mkdir(parents=True, exist_ok=True)
    digest = capability / "digest.txt"
    digest.write_text(DIGEST, encoding="utf-8")
    stamp = NOW - age_minutes * 60
    os.utime(digest, (stamp, stamp))
    return digest


def decide(
    root: Path, state: Path, **updates: object
) -> ModelSessionCapabilityDigestResult:
    base: dict[str, object] = {
        "cwd": str(root / "repo"),
        "registry_roots": str(root),
        "state_dir": str(state),
        "now_epoch": NOW,
        "uid": 501,
    }
    return HandlerSessionCapabilityDigest().handle(
        ModelSessionCapabilityDigestRequest(**(base | updates))
    )


@pytest.fixture
def layout(tmp_path: Path) -> tuple[Path, Path]:
    root = tmp_path / "registry"
    (root / "repo").mkdir(parents=True)
    state = tmp_path / "state"
    state.mkdir()
    return root, state


def test_digest_printed_inside_root(layout: tuple[Path, Path]) -> None:
    root, state = layout
    write_digest(state, age_minutes=5)
    result = decide(root, state)
    assert result.outcome is EnumSessionCapabilityDigestOutcome.DIGEST
    assert result.output.startswith("CAPABILITIES generated_at=")
    assert result.age_minutes == 5


def test_silent_outside_roots(layout: tuple[Path, Path], tmp_path: Path) -> None:
    root, state = layout
    write_digest(state, age_minutes=5)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    result = decide(root, state, cwd=str(elsewhere))
    assert result.outcome is EnumSessionCapabilityDigestOutcome.SILENT_OUTSIDE_ROOTS
    assert result.output == ""
    # A sibling whose name starts with the root's name is not inside it.
    sibling = tmp_path / "registry-other"
    sibling.mkdir()
    assert decide(root, state, cwd=str(sibling)).output == ""
    # No roots at all means no root to be inside.
    assert decide(root, state, registry_roots="").output == ""


def test_stale_prints_fix(layout: tuple[Path, Path]) -> None:
    root, state = layout
    write_digest(state, age_minutes=46)
    (state / "capability" / "failure.json").write_text(
        json.dumps({"failed_at": "x", "reason": "collector\nfailed", "traceback": "t"}),
        encoding="utf-8",
    )
    result = decide(root, state)
    assert result.outcome is EnumSessionCapabilityDigestOutcome.STALE
    lines = result.output.splitlines()
    assert lines[0] == "CAPABILITY DIGEST STALE age=46m"
    assert lines[1] == "reason: collector failed"
    assert (
        lines[2] == "fix: launchctl kickstart -k gui/501/ai.omninode.capability-catalog"
    )
    assert "CAPABILITIES generated_at=" not in result.output


def test_stale_prints_fix_when_digest_missing(layout: tuple[Path, Path]) -> None:
    root, state = layout
    result = decide(root, state)
    assert result.outcome is EnumSessionCapabilityDigestOutcome.STALE
    assert result.output.splitlines()[0] == "CAPABILITY DIGEST STALE age=unknown"
    assert result.output.splitlines()[-1].startswith("fix: launchctl kickstart -k gui/")
    assert (
        decide(root, state, state_dir="").output.count("state directory is not set")
        == 1
    )


def test_fresh_boundary_is_45_minutes(layout: tuple[Path, Path]) -> None:
    root, state = layout
    write_digest(state, age_minutes=45)
    assert decide(root, state).outcome is EnumSessionCapabilityDigestOutcome.DIGEST


def test_compact_restart_prints_one_pointer_line(layout: tuple[Path, Path]) -> None:
    root, state = layout
    write_digest(state, age_minutes=1)
    result = decide(root, state, source="compact")
    assert result.outcome is EnumSessionCapabilityDigestOutcome.COMPACT_POINTER
    assert result.output.count("\n") == 0
    assert "onex-capability-catalog digest" in result.output
    assert decide(root, state, source="compact", cwd="/").output == ""


def run_cli(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    stdin: str,
    env: dict[str, str],
) -> tuple[int, str]:
    for name in ("ONEX_REGISTRY_ROOTS", "OMNI_HOME", "ONEX_STATE_DIR"):
        monkeypatch.delenv(name, raising=False)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr("sys.stdin", io.StringIO(stdin))
    code = cli.main()
    return code, capsys.readouterr().out


def test_exits_zero_always(
    layout: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root, state = layout
    env = {"ONEX_REGISTRY_ROOTS": str(root), "ONEX_STATE_DIR": str(state)}
    inside = json.dumps({"cwd": str(root / "repo"), "source": "startup"})
    # Stale, missing and fresh digests, garbage input, a non-object document and a
    # document whose fields break the request model: all exit 0.
    for stdin in (inside, "", "not json", "[1, 2]", json.dumps({"cwd": ["x"]})):
        code, _ = run_cli(monkeypatch, capsys, stdin, env)
        assert code == 0
    write_digest(state, age_minutes=1)
    code, out = run_cli(monkeypatch, capsys, inside, env)
    assert code == 0
    assert out.startswith("CAPABILITIES generated_at=")
    code, out = run_cli(monkeypatch, capsys, inside, {})
    assert code == 0
    assert out == ""

    def boom(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("handler failure")

    monkeypatch.setattr(HandlerSessionCapabilityDigest, "handle", boom)
    code, out = run_cli(monkeypatch, capsys, inside, env)
    assert code == 0
    assert out == ""


def test_never_opens_a_socket(
    layout: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    root, state = layout
    write_digest(state, age_minutes=1)

    def refuse(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("the digest hook opened a socket")

    monkeypatch.setattr(socket, "socket", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    assert decide(root, state).outcome is EnumSessionCapabilityDigestOutcome.DIGEST
    assert decide(root, state, state_dir=str(state / "absent")).output


def test_contract_declares_its_topics_and_handler() -> None:
    contract = yaml.safe_load((NODE / "contract.yaml").read_text(encoding="utf-8"))
    assert contract["node_type"] == "effect"
    assert contract["event_bus"]["subscribe_topics"] == [
        "onex.cmd.omnimarket.session-capability-digest-print.v1"
    ]
    assert contract["event_bus"]["publish_topics"] == [
        "onex.evt.omnimarket.session-capability-digest-printed.v1"
    ]
    assert contract["handler"]["class"] == "HandlerSessionCapabilityDigest"
