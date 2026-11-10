# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The pool refuses the next lane start on a label that already runs the overlay's cap of lanes (OMN-23636).

On 2026-11-10 one account carried 97 lane attempts and 52 lane-hours in 1 h 47 min: nothing bounded how
many lanes ran on it at once. ``resolve`` now holds the caller's count of running lanes per label against
the overlay's ``steering.max_lanes_per_label`` and answers a typed refusal, and no token, once every usable
label is at the cap. The cap has no value in code: unset, the answer is what it was. Tokens are fake.
"""

from __future__ import annotations

import io
from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml
from pydantic import SecretStr, ValidationError

from omnimarket.inference.local_byok_credential_adapter import LocalByokCredentialStore
from omnimarket.nodes.node_claude_token_pool_effect import (
    runtime_claude_token_pool as cli,
)
from omnimarket.nodes.node_claude_token_pool_effect.handlers.handler_claude_token_pool import (
    HandlerClaudeTokenPool,
)
from omnimarket.nodes.node_claude_token_pool_effect.models.model_claude_token_pool import (
    ModelClaudeTokenPoolRefusal,
    ModelClaudeTokenPoolRequest,
    ModelClaudeTokenPoolResult,
    ModelPoolLabelLanes,
)
from omnimarket.nodes.node_claude_token_pool_effect.models.model_claude_token_rotation import (
    ModelRotationOverlay,
)

pytestmark = pytest.mark.unit

T0 = datetime(2026, 11, 10, 21, 0, tzinfo=UTC)
NODE = (
    Path(__file__).resolve().parents[3]
    / "src"
    / "omnimarket"
    / "nodes"
    / "node_claude_token_pool_effect"
)


def fake_token(seed: str) -> str:
    return "sk-ant-" + "oat01-" + (seed * 64)[:96]


def pool(
    tmp: Path, *labels: str, cap: int | None = None, cap_read: bool = True
) -> HandlerClaudeTokenPool:
    handler = HandlerClaudeTokenPool(
        store=LocalByokCredentialStore(db_path=tmp / "store" / "evidence.sqlite"),
        status_path=tmp / "cache" / "claude-token-pool.json",
        host="h-test",
        now=lambda: T0,
        lane_cap=cap,
        lane_cap_read=cap_read,
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


def lanes(**running: int) -> tuple[ModelPoolLabelLanes, ...]:
    return tuple(
        ModelPoolLabelLanes(label=label.replace("_", "-"), lanes=count)
        for label, count in running.items()
    )


def resolve(
    handler: HandlerClaudeTokenPool,
    counts: tuple[ModelPoolLabelLanes, ...] | None,
    *drain: str,
) -> ModelClaudeTokenPoolResult:
    return handler.handle(
        ModelClaudeTokenPoolRequest(
            operation="resolve", drain_labels=drain, lane_counts=counts
        )
    )


def test_the_cap_refuses_the_next_start_with_a_typed_refusal(tmp_path: Path) -> None:
    handler = pool(tmp_path, "acct-a", "acct-b", cap=2)
    drawn = resolve(handler, lanes(acct_a=2, acct_b=2))
    assert drawn.chosen is None
    assert drawn.token is None
    assert isinstance(drawn.refusal, ModelClaudeTokenPoolRefusal)
    assert drawn.refusal.code == "label_lane_cap"
    assert drawn.refusal.cap == 2
    assert drawn.refusal.labels == ("acct-a", "acct-b")


def test_one_lane_under_the_cap_is_served(tmp_path: Path) -> None:
    # The failing control of the test above: one lane fewer on acct-b and the start is served.
    handler = pool(tmp_path, "acct-a", "acct-b", cap=2)
    drawn = resolve(handler, lanes(acct_a=2, acct_b=1))
    assert (drawn.chosen, drawn.refusal) == ("acct-b", None)


def test_a_label_at_the_cap_is_passed_over_for_a_drained_label_under_it(
    tmp_path: Path,
) -> None:
    handler = pool(tmp_path, "acct-a", "acct-b", cap=2)
    drawn = resolve(handler, lanes(acct_a=0, acct_b=2), "acct-a")
    assert (drawn.chosen, drawn.drained_fallback) == ("acct-a", True)


def test_a_refused_start_counts_no_use_and_moves_no_current_label(
    tmp_path: Path,
) -> None:
    handler = pool(tmp_path, "acct-a", cap=1)
    refused = resolve(handler, lanes(acct_a=1))
    assert refused.refusal is not None
    assert refused.tokens[0].uses == 0


def test_with_the_cap_unset_any_number_of_lanes_is_served(tmp_path: Path) -> None:
    handler = pool(tmp_path, "acct-a")
    drawn = resolve(handler, lanes(acct_a=500))
    assert (drawn.chosen, drawn.refusal) == ("acct-a", None)
    assert resolve(handler, None).chosen == "acct-a"


def test_error_chain_a_cap_with_no_counts_is_refused_not_ignored(
    tmp_path: Path,
) -> None:
    handler = pool(tmp_path, "acct-a", cap=3)
    drawn = resolve(handler, None)
    assert drawn.chosen is None
    assert drawn.refusal is not None
    assert (drawn.refusal.code, drawn.refusal.cap) == ("lane_counts_missing", 3)


def test_error_chain_counts_with_an_unread_overlay_are_refused(tmp_path: Path) -> None:
    handler = pool(tmp_path, "acct-a", cap_read=False)
    drawn = resolve(handler, lanes(acct_a=0))
    assert drawn.chosen is None
    assert drawn.refusal is not None
    assert drawn.refusal.code == "lane_cap_unread"
    # A caller that passed no counts is served as before: the host has no overlay and no cap.
    assert resolve(handler, None).chosen == "acct-a"


def test_error_chain_a_limited_pool_is_not_a_cap_refusal(tmp_path: Path) -> None:
    handler = pool(tmp_path, "acct-a", cap=1)
    handler.handle(
        ModelClaudeTokenPoolRequest(
            operation="mark_limited", label="acct-a", seconds=3600
        )
    )
    drawn = resolve(handler, lanes(acct_a=1))
    assert drawn.refusal is None
    assert drawn.all_limited is True


class _Pipe(io.StringIO):
    def isatty(self) -> bool:
        return False


def test_error_chain_the_cli_exits_5_and_names_the_refusal_and_no_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    handler = pool(tmp_path, "acct-a", cap=1)
    out, err = _Pipe(), _Pipe()
    monkeypatch.setattr("sys.stdout", out)
    monkeypatch.setattr("sys.stderr", err)
    code = cli.main(["resolve", "--lanes", "acct-a=1"], handler=handler)
    assert code == cli.EXIT_START_REFUSED == 5
    assert out.getvalue() == ""
    assert err.getvalue().rstrip().endswith("cap=1 refusal=label_lane_cap")
    assert fake_token("a") not in err.getvalue()


def test_error_chain_a_malformed_lanes_flag_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    handler = pool(tmp_path, "acct-a")
    monkeypatch.setattr("sys.stdout", _Pipe())
    err = _Pipe()
    monkeypatch.setattr("sys.stderr", err)
    assert cli.main(["resolve", "--lanes", "acct-a=many"], handler=handler) == 2
    assert "not <label>=<number>" in err.getvalue()


def test_the_cap_is_read_from_the_overlay_and_from_nowhere_else(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    example = yaml.safe_load((NODE / "overlay.example.yaml").read_text("utf-8"))
    root = tmp_path / "roots"
    (root / "node_claude_token_pool_effect").mkdir(parents=True)
    target = root / "node_claude_token_pool_effect" / "overlay.yaml"

    monkeypatch.setenv("ONEX_SKILL_OVERLAY_ROOTS", str(tmp_path / "empty"))
    assert cli.read_lane_cap() == (None, False)

    monkeypatch.setenv("ONEX_SKILL_OVERLAY_ROOTS", str(root))
    example["steering"]["max_lanes_per_label"] = 7
    target.write_text(yaml.safe_dump(example), encoding="utf-8")
    assert cli.read_lane_cap() == (7, True)

    del example["steering"]["max_lanes_per_label"]
    target.write_text(yaml.safe_dump(example), encoding="utf-8")
    assert cli.read_lane_cap() == (None, True)

    del example["steering"]
    target.write_text(yaml.safe_dump(example), encoding="utf-8")
    assert cli.read_lane_cap() == (None, True)

    target.write_text("hosts: 3\n", encoding="utf-8")
    assert cli.read_lane_cap() == (None, False)


def overlay_with(steering: dict[str, object] | None) -> ModelRotationOverlay:
    document = yaml.safe_load((NODE / "overlay.example.yaml").read_text("utf-8"))
    if steering is None:
        document.pop("steering")
    else:
        document["steering"] = steering
    return ModelRotationOverlay.model_validate(document)


def test_the_example_overlay_declares_the_steering_block() -> None:
    document = yaml.safe_load((NODE / "overlay.example.yaml").read_text("utf-8"))
    steering = ModelRotationOverlay.model_validate(document).steering
    assert steering is not None
    assert steering.enabled is True
    assert steering.max_lanes_per_label == document["steering"]["max_lanes_per_label"]


def test_the_shipped_shape_is_the_switch_off_and_no_cap() -> None:
    assert overlay_with(None).steering is None
    off = overlay_with({"enabled": False}).steering
    assert off is not None
    assert (off.enabled, off.max_lanes_per_label) == (False, None)


def test_the_overlay_refuses_a_cap_with_the_switch_off_and_a_switch_with_no_values() -> (
    None
):
    with pytest.raises(ValidationError, match="max_lanes_per_label"):
        overlay_with({"enabled": False, "max_lanes_per_label": 4})
    with pytest.raises(ValidationError, match="verdict_argv"):
        overlay_with({"enabled": True})
    with pytest.raises(ValidationError):
        overlay_with({"enabled": False, "lane_cap": 4})
    with pytest.raises(ValidationError):
        overlay_with(
            {
                "enabled": True,
                "max_lanes_per_label": 0,
                "verdict_argv": ["x"],
                "verdict_timeout_seconds": 5,
                "open_claim_max_age_seconds": 3600,
            }
        )
