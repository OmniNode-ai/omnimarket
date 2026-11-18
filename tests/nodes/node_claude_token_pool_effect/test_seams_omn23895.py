# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Seam tests for the limit recheck and the store adopt (OMN-23895): each fails when either side changes shape.

1. the probe's typed receipt (``ModelClaudeCredentialProbeReceipt``, shared with the refill and the delegation call
   node) -> the pool's headless probe;
2. the pool's ``export --name`` line -> the secret seed node's dotenv reader;
3. the account-limit handler's ``mark-limited ... --exact`` command line -> the pool CLI's parser;
4. the overlay's ``limit_recheck`` block -> the overlay model (absence tolerated, a bad block refused).
All four run in memory: no bus topic is declared by either node, so there is no broker leg.
"""

from __future__ import annotations

import io
import subprocess
from pathlib import Path

import pytest
from omnibase_infra.nodes.node_secret_seed_effect.handlers.handler_secret_seed import (
    parse_source,
)
from pydantic import SecretStr

from omnimarket.inference.local_byok_credential_adapter import LocalByokCredentialStore
from omnimarket.models.delegation.model_claude_credential_probe import (
    ModelClaudeCredentialProbeReceipt,
)
from omnimarket.nodes.node_claude_token_pool_effect import (
    runtime_claude_token_pool as cli,
)
from omnimarket.nodes.node_claude_token_pool_effect.handlers.adapter_limit_probe import (
    HeadlessLimitProbe,
)
from omnimarket.nodes.node_claude_token_pool_effect.handlers.handler_claude_token_pool import (
    HandlerClaudeTokenPool,
)
from omnimarket.nodes.node_claude_token_pool_effect.models.model_claude_token_pool import (
    ModelClaudeTokenPoolRequest,
)
from omnimarket.nodes.node_claude_token_pool_effect.models.model_claude_token_rotation import (
    ModelLimitProbeSettings,
)
from omnimarket.nodes.node_lane_placement_recovery_effect.handlers.handler_claude_account_limit import (
    HandlerClaudeAccountLimit,
)
from tests.nodes.node_claude_token_pool_effect.test_limit_recheck import overlay

pytestmark = pytest.mark.unit

TOKEN = "sk-ant-" + "oat01-" + "y" * 90


def test_seam_probe_receipt_built_from_the_shared_model_is_read_by_the_probe() -> None:
    settings = ModelLimitProbeSettings(
        model="m",
        prompt="p",
        timeout_seconds=5,
        limited_patterns=("hit your weekly limit",),
        reset_pattern=r"resets (?P<month>[A-Za-z]{3}) (?P<day>\d+), (?P<hour>\d+)(?P<ampm>am|pm)",
        reset_margin_seconds=0,
    )
    wire = ModelClaudeCredentialProbeReceipt(
        output="ok", wall_seconds=1.0, model="m", exit_cause="answered", exit_code=0
    ).model_dump_json()
    probe = HeadlessLimitProbe(
        secret=lambda _label: SecretStr(TOKEN),
        settings=settings,
        lane_overlay_yaml=lambda: "x: 1\n",
        run=lambda _argv, _stdin, _env: (0, wire),
    )
    assert probe.probe("onexbot").verdict == "ok"


def test_seam_export_name_line_is_read_by_the_seed_nodes_dotenv_reader(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = LocalByokCredentialStore(db_path=tmp_path / "store" / "e.sqlite")
    pool = HandlerClaudeTokenPool(
        store=store, status_path=tmp_path / "mirror.json", host="h053"
    )
    pool.handle(
        ModelClaudeTokenPoolRequest(
            operation="add", label="onexbot", setup_token_output=SecretStr(TOKEN)
        )
    )
    monkeypatch.setattr(cli, "HandlerClaudeTokenPool", lambda **_: pool)
    sink = io.StringIO()
    monkeypatch.setattr("sys.stdout", sink)
    monkeypatch.setattr(sink, "isatty", lambda: False, raising=False)
    rc = cli.main(
        ["export", "--label", "onexbot", "--name", "CLAUDE_CODE_OAUTH_ONEXBOT"],
        handler=pool,
    )
    assert rc == 0
    parsed = parse_source(sink.getvalue())
    assert list(parsed) == ["CLAUDE_CODE_OAUTH_ONEXBOT"]
    assert parsed["CLAUDE_CODE_OAUTH_ONEXBOT"].get_secret_value() == TOKEN


def test_seam_export_refuses_a_terminal(monkeypatch: pytest.MonkeyPatch) -> None:
    sink = io.StringIO()
    monkeypatch.setattr(sink, "isatty", lambda: True, raising=False)
    monkeypatch.setattr("sys.stdout", sink)
    assert cli.main(["export", "--label", "onexbot"]) == cli.EXIT_REFUSED
    assert sink.getvalue() == ""


def test_seam_the_exact_mark_command_parses_in_the_pool_cli() -> None:
    scripts: list[str] = []

    def capture(argv: object, timeout: float) -> subprocess.CompletedProcess[str]:
        scripts.append(str(argv))
        return subprocess.CompletedProcess([], 0, "", "")

    handler = HandlerClaudeAccountLimit(
        {
            "CLAUDE_POOL_SH": ":",
            "CLAUDE_POOL_MODULE": "m",
            "exec_argv": lambda _host, _name, script: script,
            "PROBE_TIMEOUT_S": 5,
        }
    )
    handler._mark_pool_account("h", "onexbot", 60.0, capture, exact=True)
    tail = scripts[0].split("mark-limited", 1)[1].split("\\n")[0].strip("'\" ")
    parsed = cli._parser().parse_args(["mark-limited", *tail.split()])
    assert (parsed.label, parsed.seconds, parsed.exact) == ("onexbot", 61.0, True)


def test_seam_overlay_without_the_limit_recheck_block_is_valid_and_a_bad_block_is_refused() -> (
    None
):
    assert overlay(recheck=False).limit_recheck is None
    assert overlay(recheck=True).limit_recheck is not None
    with pytest.raises(ValueError, match="limit_recheck"):
        type(overlay()).model_validate(
            {
                **overlay().model_dump(mode="json"),
                "limit_recheck": {"policy": {"interval_seconds": 1}},
            }
        )
