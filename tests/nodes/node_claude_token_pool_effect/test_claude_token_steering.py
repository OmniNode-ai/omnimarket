# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A new lane start stays off a draining label while another is usable (OMN-23636).

On 2026-11-10 a newly added account was used up in 1 h 47 min because every lane start drew it: the
pool handed out the first unlimited label whatever its usage. ``resolve`` now takes the usage verdict's
``drain_labels``: a label in the list is skipped while any other label is usable, is used when it is the
only one left, and a limited or revoked label is never returned whatever the list says. Every token here
is fake and assembled at run time.
"""

from __future__ import annotations

import io
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import SecretStr

from omnimarket.inference.local_byok_credential_adapter import LocalByokCredentialStore
from omnimarket.nodes.node_claude_token_pool_effect import (
    runtime_claude_token_pool as cli,
)
from omnimarket.nodes.node_claude_token_pool_effect.handlers.handler_claude_token_pool import (
    HandlerClaudeTokenPool,
)
from omnimarket.nodes.node_claude_token_pool_effect.models.model_claude_token_pool import (
    ModelClaudeTokenPoolRequest,
    ModelClaudeTokenPoolResult,
)

pytestmark = pytest.mark.unit

T0 = datetime(2026, 11, 10, 21, 0, tzinfo=UTC)


def fake_token(seed: str) -> str:
    return "sk-ant-" + "oat01-" + (seed * 64)[:96]


class Clock:
    def __init__(self) -> None:
        self.at = T0

    def __call__(self) -> datetime:
        return self.at


def pool(tmp: Path, clock: Clock, *labels: str) -> HandlerClaudeTokenPool:
    handler = HandlerClaudeTokenPool(
        store=LocalByokCredentialStore(db_path=tmp / "store" / "evidence.sqlite"),
        status_path=tmp / "cache" / "claude-token-pool.json",
        host="h-test",
        now=clock,
    )
    for label in labels:
        handler.handle(
            ModelClaudeTokenPoolRequest(
                operation="add",
                label=label,
                setup_token_output=SecretStr(fake_token(label[-1])),
            )
        )
    return handler


def resolve(handler: HandlerClaudeTokenPool, *drain: str) -> ModelClaudeTokenPoolResult:
    return handler.handle(
        ModelClaudeTokenPoolRequest(operation="resolve", drain_labels=drain)
    )


def test_a_drained_label_is_skipped_while_another_label_is_usable(
    tmp_path: Path,
) -> None:
    handler = pool(tmp_path, Clock(), "acct-a", "acct-b")
    drawn = resolve(handler, "acct-a")
    assert drawn.chosen == "acct-b"
    assert drawn.drained_fallback is False
    assert drawn.token is not None


def test_the_same_pool_draws_its_first_label_with_nothing_drained(
    tmp_path: Path,
) -> None:
    # The failing control of the test above: the skip is the drain list's doing.
    handler = pool(tmp_path, Clock(), "acct-a", "acct-b")
    assert resolve(handler).chosen == "acct-a"


def test_a_drained_label_is_used_when_it_is_the_only_one(tmp_path: Path) -> None:
    handler = pool(tmp_path, Clock(), "acct-a")
    drawn = resolve(handler, "acct-a")
    assert drawn.chosen == "acct-a"
    assert drawn.drained_fallback is True


def test_a_drained_label_is_used_when_every_other_label_is_limited(
    tmp_path: Path,
) -> None:
    handler = pool(tmp_path, Clock(), "acct-a", "acct-b")
    handler.handle(
        ModelClaudeTokenPoolRequest(
            operation="mark_limited", label="acct-b", seconds=3600
        )
    )
    drawn = resolve(handler, "acct-a")
    assert (drawn.chosen, drawn.drained_fallback) == ("acct-a", True)


def test_a_limited_label_is_never_returned_drained_or_not(tmp_path: Path) -> None:
    handler = pool(tmp_path, Clock(), "acct-a", "acct-b")
    for label in ("acct-a", "acct-b"):
        handler.handle(
            ModelClaudeTokenPoolRequest(
                operation="mark_limited", label=label, seconds=3600
            )
        )
    for drain in ((), ("acct-a",), ("acct-a", "acct-b")):
        drawn = resolve(handler, *drain)
        assert drawn.chosen is None
        assert drawn.token is None
        assert drawn.refusal is None  # nothing usable is the old answer, not a refusal


def test_a_revoked_label_is_never_returned_as_the_drained_fallback(
    tmp_path: Path,
) -> None:
    handler = pool(tmp_path, Clock(), "acct-a", "acct-b")
    handler.handle(
        ModelClaudeTokenPoolRequest(operation="mark_revoked", label="acct-a")
    )
    drawn = resolve(handler, "acct-a", "acct-b")
    assert (drawn.chosen, drawn.drained_fallback) == ("acct-b", True)


def test_drained_labels_keep_the_draw_order_among_themselves(tmp_path: Path) -> None:
    handler = pool(tmp_path, Clock(), "acct-a", "acct-b", "acct-c")
    assert resolve(handler, "acct-a", "acct-b").chosen == "acct-c"
    handler.handle(
        ModelClaudeTokenPoolRequest(
            operation="mark_limited", label="acct-c", seconds=3600
        )
    )
    assert resolve(handler, "acct-b", "acct-a").chosen == "acct-a"


def test_a_label_the_pool_does_not_hold_in_the_drain_list_changes_nothing(
    tmp_path: Path,
) -> None:
    handler = pool(tmp_path, Clock(), "acct-a")
    drawn = resolve(handler, "acct-z")
    assert (drawn.chosen, drawn.drained_fallback) == ("acct-a", False)


class _Pipe(io.StringIO):
    def isatty(self) -> bool:
        return False


def test_golden_chain_a_filling_account_is_drained_then_used_then_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The chain a switched-on lane start walks through the CLI, the surface the host runs.

    add acct-a, acct-b -> resolve --drain-labels acct-a draws acct-b (the line ends label=acct-b)
    acct-b hits its limit -> the same resolve draws acct-a and says drained_fallback=1
    acct-b's reset passes -> the same resolve draws acct-b again
    No output carries a token outside the one line resolve writes to its captured stdout.
    """
    clock = Clock()
    handler = pool(tmp_path, clock, "acct-a", "acct-b")

    def run(*argv: str) -> tuple[int, str, str]:
        out, err = _Pipe(), _Pipe()
        monkeypatch.setattr("sys.stdout", out)
        monkeypatch.setattr("sys.stderr", err)
        code = cli.main(list(argv), handler=handler)
        return code, out.getvalue(), err.getvalue()

    code, out, err = run("resolve", "--drain-labels", "acct-a", "--lanes", "")
    assert code == 0
    assert out == fake_token("b")
    assert err.rstrip().endswith("label=acct-b")
    assert "drained_fallback" not in err

    assert run("mark-limited", "--label", "acct-b", "--seconds", "3600")[0] == 0
    code, out, err = run("resolve", "--drain-labels", "acct-a", "--lanes", "")
    assert code == 0
    assert out == fake_token("a")
    assert err.rstrip().endswith("drained_fallback=1 label=acct-a")
    assert fake_token("a") not in err

    clock.at = T0 + timedelta(seconds=3601)
    code, out, err = run("resolve", "--drain-labels", "acct-a", "--lanes", "")
    assert (code, out) == (0, fake_token("b"))
    assert err.rstrip().endswith("label=acct-b")


def test_a_resolve_with_no_steering_flags_is_the_request_it_was(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[ModelClaudeTokenPoolRequest] = []
    handler = pool(tmp_path, Clock(), "acct-a")
    real = handler.handle

    def record(request: ModelClaudeTokenPoolRequest) -> ModelClaudeTokenPoolResult:
        seen.append(request)
        return real(request)

    monkeypatch.setattr(handler, "handle", record)
    monkeypatch.setattr("sys.stdout", _Pipe())
    monkeypatch.setattr("sys.stderr", _Pipe())
    assert cli.main(["resolve"], handler=handler) == 0
    assert seen[-1].drain_labels == ()
    assert seen[-1].lane_counts is None
