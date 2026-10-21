# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Pure billing classification and presentation tests."""

import json
from datetime import date
from pathlib import Path

import pytest
from pydantic import ValidationError

from omnimarket.nodes.node_private_hosted_minutes_watch_effect.handlers.handler_hosted_minutes_tally import (
    billable_hosted_minutes,
    daily_totals,
    month_by_repo,
    render_alert,
    render_status_row,
)
from omnimarket.nodes.node_private_hosted_minutes_watch_effect.models import (
    ModelHostedMinutesWatchRequest,
    ModelHostedMinutesWatchResult,
    ModelRepoDayMinutes,
)

FIXTURES = Path(__file__).parent / "fixtures"
DAY = date(2026, 10, 18)


def test_fixture_tallies_repo_days_and_excludes_free_and_non_hosted():
    rows = billable_hosted_minutes(
        json.loads((FIXTURES / "billing_usage_month.json").read_text())["usageItems"]
    )
    assert [(r.repository, r.day.isoformat(), r.minutes) for r in rows] == [
        ("private-repo-a", "2026-10-17", 12.5),
        ("private-repo-a", "2026-10-18", 6),
        ("private-repo-b", "2026-10-18", 20.25),
    ]
    assert rows[0].workflows == (".github/workflows/ci.yml",)
    assert rows[0].skus == ("actions_linux",)
    assert rows[2].workflows == ("macos-ci",)
    assert month_by_repo(rows) == {"private-repo-a": 18.5, "private-repo-b": 20.25}
    assert daily_totals(rows) == {"2026-10-17": 12.5, "2026-10-18": 26.25}
    assert list(month_by_repo(reversed(rows))) == ["private-repo-a", "private-repo-b"]
    assert list(daily_totals(reversed(rows))) == ["2026-10-17", "2026-10-18"]


def test_workflows_and_skus_are_sorted_unique_and_case_insensitive():
    base = {
        "repositoryName": "private-repo-a",
        "date": "2026-10-18T12:34:00Z",
        "product": "aCtIoNs",
        "unitType": "mInUtEs",
        "quantity": "1.125",
        "grossAmount": "0.1",
        "sku": "actions_linux",
    }
    rows = billable_hosted_minutes(
        [
            dict(base, workflowPath="z", workflowName="a", workflow="b"),
            dict(base, workflowPath="a", sku="actions_macos"),
        ]
    )
    assert rows[0].minutes == 2.25
    assert rows[0].workflows == ("a", "b", "z")
    assert rows[0].skus == ("actions_linux", "actions_macos")
    assert month_by_repo(rows) == {"private-repo-a": 2.25}


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("product", "Copilot"),
        ("unitType", "Bytes"),
        ("sku", "Actions_SELF_HOSTED"),
        ("sku", "Actions_SELF-HOSTED"),
        ("quantity", 0),
        ("quantity", -1),
        ("quantity", None),
        ("quantity", "bad"),
        ("quantity", "NaN"),
        ("quantity", "inf"),
        ("grossAmount", 0),
        ("grossAmount", -1),
        ("grossAmount", None),
        ("grossAmount", "bad"),
        ("repositoryName", ""),
        ("repositoryName", None),
    ],
)
def test_exclusions_and_malformed_numbers(field, value):
    base = {
        "repositoryName": "private-repo-a",
        "date": "2026-10-18T00:00:00Z",
        "product": "Actions",
        "unitType": "Minutes",
        "quantity": 1,
        "grossAmount": 1,
        "sku": "actions_linux",
    }
    assert billable_hosted_minutes([dict(base, **{field: value})]) == []
    base.pop(field)
    if field in {"quantity", "grossAmount", "repositoryName"}:
        assert billable_hosted_minutes([base]) == []


@pytest.mark.parametrize("items", [None, {}, "wrong"])
def test_non_list_usage_items_rejected(items):
    with pytest.raises(ValueError, match="usageItems"):
        billable_hosted_minutes(items)


def test_alert_and_status_exact_shape():
    rows = [
        ModelRepoDayMinutes(
            repository="private-repo-b",
            day=DAY,
            minutes=20.25,
            skus=(),
            workflows=("macos-ci",),
        ),
        ModelRepoDayMinutes(
            repository="private-repo-a", day=DAY, minutes=6, skus=(), workflows=()
        ),
    ]
    assert render_alert("OmniNode-ai", DAY, rows) == (
        "GitHub-hosted Actions minutes on private repositories, 2026-10-18 UTC (rule: zero): "
        "private-repo-b 20.25 min (workflows: macos-ci), private-repo-a 6 min; total 26.25 min. Org OmniNode-ai."
    )
    assert render_status_row(
        "2026-10-19T12:00:00Z",
        DAY,
        rows,
        26.25,
        "sent",
        8,
        {"private-repo-a": 18.5, "private-repo-b": 20.25},
    ) == (
        "2026-10-19T12:00:00Z | STATUS | lane=private-hosted-minutes-watch | ticket=OMN-17427 | "
        "actor=launchd:private-hosted-minutes-watch | day=2026-10-18 | hosted_minutes=26.25 | "
        "repos=private-repo-b:20.25,private-repo-a:6 | alert=sent | private-repository GitHub-hosted Actions minutes "
        "for 2026-10-18 UTC from one billing usage read (8 usage items); month to date by repository: private-repo-a:18.5,private-repo-b:20.25"
    )


def test_rendering_rounds_and_sanitizes_pipes():
    rows = [
        ModelRepoDayMinutes(
            repository="private-repo-a|",
            day=DAY,
            minutes=1.234,
            skus=(),
            workflows=("a|b",),
        )
    ]
    alert = render_alert("OmniNode-ai", DAY, rows)
    assert "private-repo-a/ 1.23 min (workflows: a/b)" in alert
    assert "|" not in alert
    status = render_status_row(
        "2026-10-19T12:00:00Z", DAY, rows, 1.234, "sent", 1, {"private-repo-a|": 1.234}
    )
    assert status.count("|") == 9
    assert "private-repo-a/:1.23" in status
    assert month_by_repo(rows) == {"private-repo-a|": 1.23}
    assert daily_totals(rows) == {"2026-10-18": 1.23}


def test_quiet_status_uses_none():
    row = render_status_row("2026-10-19T12:00:00Z", DAY, [], 0, "none", 2, {})
    assert "hosted_minutes=0 | repos=none | alert=none" in row
    assert row.endswith("month to date by repository: none")


@pytest.mark.parametrize(
    "model",
    [
        ModelHostedMinutesWatchRequest,
        ModelHostedMinutesWatchResult,
        ModelRepoDayMinutes,
    ],
)
def test_models_are_frozen_and_forbid_extras(model):
    assert model.model_config["frozen"] is True
    assert model.model_config["extra"] == "forbid"
    with pytest.raises(ValidationError):
        model(unexpected=True)
