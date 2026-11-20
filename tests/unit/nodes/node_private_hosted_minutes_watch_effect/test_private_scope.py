# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The watch counts private repositories only, against the overlay's included minutes."""

import json
import re
from datetime import UTC, date, datetime
from pathlib import Path
from unittest.mock import Mock

import pytest

from omnimarket.handlers.node_overlay_reader import NodeOverlayError
from omnimarket.nodes.node_private_hosted_minutes_watch_effect import __main__ as cli
from omnimarket.nodes.node_private_hosted_minutes_watch_effect.handlers import (
    handler_private_hosted_minutes_watch as module,
)
from omnimarket.nodes.node_private_hosted_minutes_watch_effect.models import (
    ModelHostedMinutesOverlay,
    ModelHostedMinutesWatchRequest,
)
from omnimarket.nodes.node_private_hosted_minutes_watch_effect.overlay import (
    load_overlay,
)

NODE = (
    Path(__file__).resolve().parents[4]
    / "src/omnimarket/nodes/node_private_hosted_minutes_watch_effect"
)
DAY = date(2026, 10, 18)
NOW = datetime(2026, 10, 19, 12, tzinfo=UTC)
OVERLAY = ModelHostedMinutesOverlay(included_minutes=50_000, alert_fraction=0.8)
VISIBILITY = {
    "private-repo-a": "private",
    "public-repo-b": "public",
    "private-repo-c": "private",
}


def usage(**minutes):
    return {
        "usageItems": [
            {
                "date": "2026-10-18T00:00:00Z",
                "product": "Actions",
                "sku": "actions_linux",
                "quantity": quantity,
                "unitType": "Minutes",
                "grossAmount": 1.0,
                "repositoryName": name.replace("_", "-"),
            }
            for name, quantity in minutes.items()
        ]
    }


def visibility_reader(mapping=None):
    table = VISIBILITY if mapping is None else mapping

    def read(org, repository):
        value = table[repository]
        if isinstance(value, Exception):
            raise value
        return value

    return Mock(side_effect=read)


def build(payload, overlay=OVERLAY, visibility=None, sender=None):
    sender = sender or Mock(return_value="sent")
    handler = module.HandlerPrivateHostedMinutesWatch(
        billing_reader=Mock(return_value=payload),
        ledger_appender=Mock(return_value=(True, "written")),
        alert_sender=sender,
        clock=lambda: NOW,
        visibility_reader=visibility or visibility_reader(),
        overlay_loader=lambda: overlay,
    )
    return handler, sender


def req(tmp_path):
    return ModelHostedMinutesWatchRequest(
        day=DAY, state_dir=tmp_path / "state", ledger=tmp_path / "ledger.md"
    )


def test_public_minutes_are_uncounted_and_raise_no_alert(tmp_path):
    handler, sender = build(usage(private_repo_a=10, public_repo_b=175_935))
    result = handler.handle(req(tmp_path))
    assert result.private_month_minutes == 10
    assert result.total_day_minutes == 10
    assert [r.repository for r in result.day_minutes] == ["private-repo-a"]
    assert [(u.repository, u.minutes, u.reason) for u in result.uncounted] == [
        ("public-repo-b", 175_935, "public")
    ]
    assert result.alert == "none"
    sender.assert_not_called()
    assert "private_month_minutes=10 |" in result.status_row
    assert "included=50000 |" in result.status_row
    assert "uncounted=175935 |" in result.status_row
    assert "hosted_minutes=10 |" in result.status_row


@pytest.mark.parametrize(("minutes", "expected"), [(39_999, "none"), (40_000, "sent")])
def test_alert_threshold_from_overlay(tmp_path, minutes, expected):
    handler, sender = build(usage(private_repo_a=minutes))
    result = handler.handle(req(tmp_path))
    assert result.alert == expected
    assert sender.call_count == (1 if expected == "sent" else 0)
    if expected == "sent":
        text = sender.call_args.args[0]
        for part in ("40000", "50000", "80", "private-repo-a"):
            assert part in text


@pytest.mark.parametrize(("minutes", "expected"), [(499, "none"), (500, "sent")])
def test_changed_overlay_moves_threshold(tmp_path, minutes, expected):
    overlay = ModelHostedMinutesOverlay(included_minutes=1000, alert_fraction=0.5)
    handler, _ = build(usage(private_repo_a=minutes), overlay=overlay)
    assert handler.handle(req(tmp_path)).alert == expected


def test_second_run_same_day_sends_nothing(tmp_path):
    handler, sender = build(usage(private_repo_a=40_000))
    assert handler.handle(req(tmp_path)).alert == "sent"
    assert handler.handle(req(tmp_path)).alert == "already-sent"
    sender.assert_called_once()


def test_unreadable_visibility_is_uncounted_never_private(tmp_path):
    table = {**VISIBILITY, "private-repo-c": RuntimeError("HTTP 403")}
    handler, sender = build(
        usage(private_repo_a=5, private_repo_c=60_000),
        visibility=visibility_reader(table),
    )
    result = handler.handle(req(tmp_path))
    assert result.private_month_minutes == 5
    assert [(u.repository, u.minutes, u.reason) for u in result.uncounted] == [
        ("private-repo-c", 60_000, "unreadable")
    ]
    assert "HTTP 403" in result.uncounted[0].detail
    assert result.alert == "none"
    sender.assert_not_called()


def test_one_visibility_read_per_repository(tmp_path):
    reader = visibility_reader()
    handler, _ = build(usage(private_repo_a=1, public_repo_b=2), visibility=reader)
    handler.handle(req(tmp_path))
    assert sorted(c.args[1] for c in reader.call_args_list) == [
        "private-repo-a",
        "public-repo-b",
    ]


def write_overlay(root, text):
    target = root / "node_private_hosted_minutes_watch_effect"
    target.mkdir(parents=True)
    (target / "overlay.yaml").write_text(text, encoding="utf-8")


def test_overlay_missing_names_the_setting():
    with pytest.raises(NodeOverlayError, match="ONEX_SKILL_OVERLAY_ROOTS"):
        load_overlay({})


@pytest.mark.parametrize("fraction", ["0", "1.5", "-0.1"])
def test_overlay_invalid_alert_fraction_refuses(tmp_path, fraction):
    write_overlay(tmp_path, f"included_minutes: 100\nalert_fraction: {fraction}\n")
    with pytest.raises(NodeOverlayError, match="ONEX_SKILL_OVERLAY_ROOTS"):
        load_overlay({"ONEX_SKILL_OVERLAY_ROOTS": str(tmp_path)})


def test_overlay_loads_values(tmp_path):
    write_overlay(tmp_path, "included_minutes: 200\nalert_fraction: 0.25\n")
    overlay = load_overlay({"ONEX_SKILL_OVERLAY_ROOTS": str(tmp_path)})
    assert (overlay.included_minutes, overlay.alert_fraction) == (200, 0.25)


def test_example_overlay_validates():
    import yaml

    raw = yaml.safe_load((NODE / "overlay.example.yaml").read_text(encoding="utf-8"))
    assert ModelHostedMinutesOverlay.model_validate(raw) == OVERLAY


def test_cli_overlay_missing_exits_nonzero_and_writes_heartbeat(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.delenv("ONEX_SKILL_OVERLAY_ROOTS", raising=False)
    monkeypatch.setattr(
        module, "read_billing_usage", Mock(return_value=usage(private_repo_a=1))
    )
    code = cli.main(
        [
            "--day",
            "2026-10-18",
            "--state-dir",
            str(tmp_path / "state"),
            "--ledger",
            str(tmp_path / "ledger.md"),
        ]
    )
    assert code != 0
    assert "ONEX_SKILL_OVERLAY_ROOTS" in capsys.readouterr().err
    heartbeat = json.loads((tmp_path / "state/last-run.json").read_text())
    assert "ONEX_SKILL_OVERLAY_ROOTS" in heartbeat["error"]


def test_no_threshold_literals_or_repository_lists_in_node_source():
    literal = re.compile(r"50000|0\.8")
    for path in NODE.rglob("*"):
        if path.suffix in {".py", ".yaml"} and path.name != "overlay.example.yaml":
            assert not literal.search(path.read_text(encoding="utf-8")), path
