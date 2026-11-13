# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The pool CLI's revoke cause and the runtime wiring of the revoke recheck (OMN-19998).

``mark-revoked --evidence-stdin`` reads the failing engine's closing output once, asks the revoke
cause compute whether it is a 403 challenge page, and stores the verdict — never the text. The
tick's line and the registry module's heartbeat JSON carry the recheck cells, and the contract and
the example overlay name the nodes that decide. Every token here is fake and built at run time.
"""

from __future__ import annotations

import io
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import SecretStr

from omnimarket.handlers.node_overlay_reader import NodeOverlayError
from omnimarket.inference.local_byok_credential_adapter import LocalByokCredentialStore
from omnimarket.nodes.node_claude_token_pool_effect import (
    runtime_claude_pool_tick,
)
from omnimarket.nodes.node_claude_token_pool_effect import (
    runtime_claude_token_pool as cli,
)
from omnimarket.nodes.node_claude_token_pool_effect.handlers.handler_claude_token_pool import (
    HandlerClaudeTokenPool,
)
from omnimarket.nodes.node_claude_token_pool_effect.models.model_claude_token_pool import (
    ModelClaudeTokenPoolRequest,
)
from omnimarket.nodes.node_claude_token_pool_effect.models.model_claude_token_rotation import (
    ModelClaudeTokenRotationResult,
    ModelRevokeRecheckOutcome,
    ModelRotationMarksSync,
    ModelRotationOverlay,
)

pytestmark = pytest.mark.unit

T0 = datetime(2026, 11, 12, 9, 0, tzinfo=UTC)
T0_STAMP = "2026-11-12T09:00:00Z"
MARKER = "synthetic challenge marker"
CHALLENGE_EVIDENCE = f"lane closed: upstream answered the {MARKER} and stopped\n"
PLAIN_EVIDENCE = "lane closed: invalid api key, authentication failed\n"
TICK_MODULE = "omnimarket.nodes.node_claude_token_pool_effect.runtime_claude_pool_tick"
NODE = (
    Path(__file__).resolve().parents[3]
    / "src"
    / "omnimarket"
    / "nodes"
    / "node_claude_token_pool_effect"
)


def fake_token(label: str) -> str:
    return "sk-ant-" + "oat01-" + (label.replace("-", "x") * 40)[:96]


class _Pipe(io.StringIO):
    def isatty(self) -> bool:
        return False


class _Closed(io.StringIO):
    """A stdin that fails the test the moment anything reads it."""

    def read(self, *_args: object, **_kw: object) -> str:
        raise AssertionError("stdin must not be read without --evidence-stdin")


def pool(tmp_path: Path, *labels: str) -> HandlerClaudeTokenPool:
    handler = HandlerClaudeTokenPool(
        store=LocalByokCredentialStore(db_path=tmp_path / "store" / "evidence.sqlite"),
        status_path=tmp_path / "cache" / "claude-token-pool.json",
        host="h202",
        now=lambda: T0,
    )
    for label in labels:
        handler.handle(
            ModelClaudeTokenPoolRequest(
                operation="add",
                label=label,
                setup_token_output=SecretStr(fake_token(label)),
            )
        )
    return handler


def view(handler: HandlerClaudeTokenPool, label: str) -> Any:
    return next(
        t
        for t in handler.handle(ModelClaudeTokenPoolRequest(operation="status")).tokens
        if t.label == label
    )


def overlay(*, revoke_recheck: bool = True) -> ModelRotationOverlay:
    body: dict[str, Any] = {
        "hosts": [
            {
                "name": name,
                "scheduler": "systemd-user",
                "venv": "omnimarket/.venv",
                "token_file": ".config/omni/claude-oauth-token",
            }
            for name in ("h053", "h101")
        ],
        "marks_source": {
            "host": "h053",
            "ssh_target": None,
            "mirror_path": ".cache/omni/claude-token-pool.json",
            "max_age_seconds": 600,
        },
        "events_file": "token-rotations.jsonl",
        "unit_environment": {},
        "schedule": {"interval_seconds": 300},
    }
    if revoke_recheck:
        body["limit_recheck"] = {
            "policy": {"interval_seconds": 3600},
            "probe": {
                "model": "sonnet",
                "prompt": "Reply with the single word ok",
                "timeout_seconds": 90,
                "limited_patterns": ["hit your weekly limit"],
                "reset_pattern": (
                    r"resets (?P<month>[A-Za-z]{3}) (?P<day>\d{1,2}), "
                    r"(?P<hour>\d{1,2})(?P<ampm>am|pm)"
                ),
                "reset_margin_seconds": 7,
            },
        }
        body["revoke_recheck"] = {
            "policy": {"interval_seconds": 1800},
            "challenge_markers": [MARKER],
        }
    return ModelRotationOverlay.model_validate(body)


def declare(
    monkeypatch: pytest.MonkeyPatch, declared: ModelRotationOverlay | Exception
) -> None:
    """Point the CLI's overlay loader at ``declared`` (or make it raise)."""
    monkeypatch.setattr(cli, "find_node_overlay", lambda _name: Path("/declared"))
    if isinstance(declared, Exception):

        def refuse() -> ModelRotationOverlay:
            raise declared

        monkeypatch.setattr(cli, "load_overlay", refuse)
    else:
        monkeypatch.setattr(cli, "load_overlay", lambda: declared)


def tick_result(**kw: Any) -> ModelClaudeTokenRotationResult:
    fields: dict[str, Any] = {
        "host": "h053",
        "rank": 0,
        "outcome": "kept",
        "in_use_state": "usable",
        "marks_sync": ModelRotationMarksSync(state="applied"),
        "stale_revoke": "evaluated",
        "revokes_cleared": ("acct-z",),
        "limit_recheck": "evaluated",
        "limits_kept": ("acct-l",),
        "revoke_recheck": "evaluated",
        "revoke_rechecks": (
            ModelRevokeRecheckOutcome(label="acct-a", outcome="cleared", probed_at=T0),
            ModelRevokeRecheckOutcome(label="acct-b", outcome="kept", probed_at=None),
        ),
        "revokes_rechecked_cleared": ("acct-a",),
        "revokes_confirmed": ("acct-b",),
        "revokes_rechecked_kept": ("acct-c",),
    }
    fields.update(kw)
    return ModelClaudeTokenRotationResult(**fields)


def test_evidence_carrying_a_declared_marker_is_recorded_challenged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    handler = pool(tmp_path, "acct-a", "onexbot")
    declare(monkeypatch, overlay())
    monkeypatch.setattr("sys.stdin", _Pipe(CHALLENGE_EVIDENCE))
    assert (
        cli.main(["mark-revoked", "--label", "acct-a", "--evidence-stdin"], handler)
        == 0
    )
    out = capsys.readouterr()
    assert view(handler, "acct-a").revoke_cause == "challenged"
    assert out.err.strip() == (
        "ALERT claude-pool host=h202 challenged=acct-a: a 403 challenge ended a "
        "lane on it; the pool tick re-tests it and clears the mark when it answers"
    )


def test_plain_auth_failure_evidence_is_recorded_revoked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    handler = pool(tmp_path, "acct-a", "onexbot")
    declare(monkeypatch, overlay())
    monkeypatch.setattr("sys.stdin", _Pipe(PLAIN_EVIDENCE))
    assert (
        cli.main(["mark-revoked", "--label", "acct-a", "--evidence-stdin"], handler)
        == 0
    )
    out = capsys.readouterr()
    assert view(handler, "acct-a").revoke_cause == "revoked"
    assert out.err.startswith("ALERT claude-pool")
    assert "revoked=acct-a" in out.err
    assert "challenged" not in out.err


def test_control_an_unreadable_overlay_records_a_revoke_and_raises_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    handler = pool(tmp_path, "acct-a", "onexbot")
    declare(
        monkeypatch,
        NodeOverlayError("the overlay file cannot be found under its roots"),
    )
    monkeypatch.setattr("sys.stdin", _Pipe(CHALLENGE_EVIDENCE))
    assert (
        cli.main(["mark-revoked", "--label", "acct-a", "--evidence-stdin"], handler)
        == 0
    )
    out = capsys.readouterr()
    assert view(handler, "acct-a").revoke_cause == "revoked"
    assert "revoked=acct-a" in out.err
    assert "challenged" not in out.err


def test_without_the_flag_the_cause_is_revoked_and_stdin_is_not_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    handler = pool(tmp_path, "acct-a", "onexbot")

    def untouched() -> ModelRotationOverlay:
        raise AssertionError("the overlay must not be read without --evidence-stdin")

    monkeypatch.setattr(cli, "load_overlay", untouched)
    monkeypatch.setattr("sys.stdin", _Closed())
    assert cli.main(["mark-revoked", "--label", "acct-a"], handler) == 0
    out = capsys.readouterr()
    assert view(handler, "acct-a").revoke_cause == "revoked"
    assert out.err.startswith("ALERT claude-pool")
    assert "revoked=acct-a" in out.err


def test_the_evidence_text_is_never_printed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    handler = pool(tmp_path, "acct-a", "onexbot")
    declare(monkeypatch, overlay())
    monkeypatch.setattr("sys.stdin", _Pipe(CHALLENGE_EVIDENCE))
    cli.main(["mark-revoked", "--label", "acct-a", "--evidence-stdin"], handler)
    out = capsys.readouterr()
    assert "upstream answered" not in out.out + out.err
    assert CHALLENGE_EVIDENCE.strip() not in out.out + out.err
    assert MARKER not in out.out + out.err


def test_read_challenge_markers_answers_an_empty_tuple_when_nothing_is_declared(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(cli, "find_node_overlay", lambda _name: None)

    def unread() -> ModelRotationOverlay:
        raise AssertionError("no overlay found: nothing is loaded")

    monkeypatch.setattr(cli, "load_overlay", unread)
    assert cli.read_challenge_markers() == ()
    monkeypatch.setattr(cli, "find_node_overlay", lambda _name: Path("/declared"))

    def refuses() -> ModelRotationOverlay:
        raise NodeOverlayError("the overlay does not validate")

    monkeypatch.setattr(cli, "load_overlay", refuses)
    assert cli.read_challenge_markers() == ()
    monkeypatch.setattr(cli, "load_overlay", lambda: overlay(revoke_recheck=False))
    assert cli.read_challenge_markers() == ()
    monkeypatch.setattr(cli, "load_overlay", lambda: overlay())
    assert cli.read_challenge_markers() == (MARKER,)


def test_the_tick_line_keeps_its_cells_and_carries_the_recheck_cells_after_the_limits(
    monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    monkeypatch.setattr(cli, "run_tick", lambda _host, **_kw: tick_result())
    assert cli.main(["tick", "--host", "h053"]) == 0
    line = capsys.readouterr().out.strip()
    assert line == (
        "claude-pool-tick host=h053 rank=0 outcome=kept in_use=usable from=none "
        "to=none marks=applied stale_revoke=evaluated limit_recheck=evaluated "
        "limits_kept=acct-l revoke_recheck=evaluated "
        "revokes_rechecked_cleared=acct-a revokes_confirmed=acct-b "
        "revokes_rechecked_kept=acct-c revokes_cleared=acct-z"
    )


def test_the_registry_tick_json_names_the_revoke_recheck_keys(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    monkeypatch.delenv(runtime_claude_pool_tick.HOST_ENV, raising=False)
    monkeypatch.setenv(runtime_claude_pool_tick.HOST_ENV, "h053")
    monkeypatch.setattr(f"{TICK_MODULE}.run_tick", lambda _host, **_kw: tick_result())
    state_dir = tmp_path / "state"
    assert runtime_claude_pool_tick.main(["--state-dir", str(state_dir)]) == 0
    line = json.loads(capsys.readouterr().out)
    assert line["revoke_recheck"] == "evaluated"
    assert line["revoke_rechecks"] == [
        {"label": "acct-a", "outcome": "cleared", "probed_at": T0_STAMP},
        {"label": "acct-b", "outcome": "kept", "probed_at": None},
    ]
    assert line["revokes_rechecked_cleared"] == ["acct-a"]
    assert line["revokes_confirmed"] == ["acct-b"]
    assert line["revokes_rechecked_kept"] == ["acct-c"]
    beat = json.loads((state_dir / "last-run.json").read_text(encoding="utf-8"))
    assert beat["result"]["revoke_rechecks"] == line["revoke_rechecks"]


def test_control_the_registry_tick_json_without_a_block_names_not_configured(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    monkeypatch.delenv(runtime_claude_pool_tick.HOST_ENV, raising=False)
    monkeypatch.setenv(runtime_claude_pool_tick.HOST_ENV, "h053")
    monkeypatch.setattr(
        f"{TICK_MODULE}.run_tick",
        lambda _host, **_kw: tick_result(
            revoke_recheck="not_configured",
            revoke_rechecks=(),
            revokes_rechecked_cleared=(),
            revokes_confirmed=(),
            revokes_rechecked_kept=(),
        ),
    )
    assert runtime_claude_pool_tick.main(["--state-dir", str(tmp_path / "state")]) == 0
    line = json.loads(capsys.readouterr().out)
    assert line["revoke_recheck"] == "not_configured"
    assert (
        line["revoke_rechecks"],
        line["revokes_rechecked_cleared"],
        line["revokes_confirmed"],
        line["revokes_rechecked_kept"],
    ) == ([], [], [], [])


def test_the_contract_names_the_compute_nodes_and_the_example_overlay_declares_the_block() -> (
    None
):
    contract = yaml.safe_load((NODE / "contract.yaml").read_text(encoding="utf-8"))
    description = contract["description"]
    assert "node_claude_pool_revoke_recheck_compute" in description
    assert "node_claude_pool_revoke_cause_compute" in description
    document = yaml.safe_load(
        (NODE / "overlay.example.yaml").read_text(encoding="utf-8")
    )
    declared = ModelRotationOverlay.model_validate(document)
    assert declared.revoke_recheck is not None
    assert (
        declared.revoke_recheck.policy.interval_seconds
        == document["revoke_recheck"]["policy"]["interval_seconds"]
    )
    assert declared.revoke_recheck.challenge_markers == (
        document["revoke_recheck"]["challenge_markers"][0],
    )
