# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Sprint scope and declared credential resolution regressions (OMN-17427)."""

from unittest.mock import MagicMock

import pytest
import yaml

from omnimarket.inference.secret_store_resolver import clear_secret_store_resolver_cache
from omnimarket.nodes.node_linear_triage.handlers.handler_linear_triage import (
    GitHubClientProtocol,
    HandlerLinearTriage,
    LinearClientProtocol,
    LinearHttpClient,
)
from omnimarket.nodes.node_linear_triage.models.model_linear_triage_state import (
    ModelLinearTriageStartCommand,
)

USES_REAL_DONE_WRITE_GATE = True


def _clients():
    linear = MagicMock(spec=LinearClientProtocol)
    linear.list_issues.return_value = {
        "data": {"issues": {"nodes": [], "pageInfo": {"hasNextPage": False}}}
    }
    github = MagicMock(spec=GitHubClientProtocol)
    return linear, github


@pytest.mark.unit
async def test_default_refuses_unscoped_sweep(monkeypatch):
    monkeypatch.delenv("LINEAR_ACTIVE_SPRINT_PROJECT_ID", raising=False)
    linear, github = _clients()
    result = await HandlerLinearTriage(client=linear, github_client=github).handle(
        ModelLinearTriageStartCommand()
    )
    assert result.status == "error"
    assert "project" in result.validation_errors[0].lower()
    linear.list_issues.assert_not_called()
    github.search_prs.assert_not_called()


@pytest.mark.unit
async def test_sprint_project_is_preserved_on_every_page():
    linear, github = _clients()
    linear.list_issues.side_effect = [
        {
            "data": {
                "issues": {
                    "nodes": [],
                    "pageInfo": {"hasNextPage": True, "endCursor": "page-2"},
                }
            }
        },
        {"data": {"issues": {"nodes": [], "pageInfo": {"hasNextPage": False}}}},
    ]
    result = await HandlerLinearTriage(client=linear, github_client=github).handle(
        ModelLinearTriageStartCommand(project_id="sprint-uuid")
    )
    assert result.status == "completed"
    assert [c.kwargs["project_id"] for c in linear.list_issues.call_args_list] == [
        "sprint-uuid",
        "sprint-uuid",
    ]
    assert linear.list_issues.call_args_list[1].kwargs["after"] == "page-2"


@pytest.mark.unit
async def test_declared_sprint_config_is_used(monkeypatch):
    monkeypatch.setenv("LINEAR_ACTIVE_SPRINT_PROJECT_ID", "configured-sprint")
    linear, github = _clients()
    await HandlerLinearTriage(client=linear, github_client=github).handle(
        ModelLinearTriageStartCommand()
    )
    assert linear.list_issues.call_args.kwargs["project_id"] == "configured-sprint"


@pytest.mark.unit
def test_http_project_filter_uses_variables():
    client = LinearHttpClient("fixture-key")
    post = MagicMock(return_value={})
    client._post = post
    client.list_issues(team="Omninode", project_id="sprint-uuid", after="cursor")
    query, variables = post.call_args.args
    assert "project: { id: { eq: $projectId } }" in query
    assert variables["projectId"] == "sprint-uuid"
    assert variables["after"] == "cursor"


@pytest.mark.unit
async def test_explicit_secret_mapping_resolves_both_keys(tmp_path, monkeypatch):
    from omnimarket.nodes.node_linear_triage.handlers import (
        handler_linear_triage as module,
    )

    monkeypatch.setenv("TRIAGE_TEST_LINEAR_KEY", "fixture-linear")
    monkeypatch.setenv("TRIAGE_TEST_GITHUB_KEY", "fixture-github")
    monkeypatch.delenv("LINEAR_API_KEY", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    config = tmp_path / "secrets.yaml"
    config.write_text(
        yaml.safe_dump(
            {
                "enable_convention_fallback": False,
                "mappings": [
                    {
                        "logical_name": "LINEAR_API_KEY",
                        "source": {
                            "source_type": "env",
                            "source_path": "TRIAGE_TEST_LINEAR_KEY",
                        },
                    },
                    {
                        "logical_name": "GITHUB_TOKEN",
                        "source": {
                            "source_type": "env",
                            "source_path": "TRIAGE_TEST_GITHUB_KEY",
                        },
                    },
                ],
            }
        )
    )
    linear, github = _clients()
    linear_factory = MagicMock(return_value=linear)
    github_factory = MagicMock(return_value=github)
    monkeypatch.setattr(module, "LinearHttpClient", linear_factory)
    monkeypatch.setattr(module, "GitHubHttpClient", github_factory)
    result = await HandlerLinearTriage().handle(
        ModelLinearTriageStartCommand(
            project_id="sprint-uuid", secret_resolver_config_path=str(config)
        )
    )
    assert result.status == "completed"
    linear_factory.assert_called_once_with("fixture-linear")
    github_factory.assert_called_once_with("fixture-github")


@pytest.mark.unit
@pytest.mark.parametrize("missing_ref", ["LINEAR_API_KEY", "GITHUB_TOKEN"])
async def test_configured_store_miss_names_ref_and_never_falls_back(
    tmp_path, monkeypatch, missing_ref
):
    monkeypatch.setenv(missing_ref, "ambient-must-not-win")
    config = tmp_path / "secrets.yaml"
    config.write_text("enable_convention_fallback: false\nmappings: []\n")
    monkeypatch.setenv("ONEX_SECRET_RESOLVER_CONFIG_PATH", str(config))
    clear_secret_store_resolver_cache()
    try:
        linear, github = _clients()
        handler = HandlerLinearTriage(
            client=linear if missing_ref == "GITHUB_TOKEN" else None,
            github_client=github if missing_ref == "LINEAR_API_KEY" else None,
        )
        result = await handler.handle(
            ModelLinearTriageStartCommand(project_id="sprint-uuid")
        )
        assert result.status == "error"
        assert missing_ref in result.validation_errors[0]
        assert "secret" in result.validation_errors[0].lower()
        assert "ambient-must-not-win" not in result.model_dump_json()
    finally:
        clear_secret_store_resolver_cache()


@pytest.mark.unit
async def test_sprint_parent_cannot_close_over_open_child_outside_sprint():
    from tests.nodes.node_linear_triage.test_omn_20368_done_write_receipt_gate import (
        _BOUND,
        _github_with_merged_implementing_pr,
        _issue,
        _linear,
        _Probe,
    )

    linear = _linear(_issue())
    linear.list_children.return_value = {
        "data": {
            "issues": {
                "nodes": [
                    {
                        "id": "outside-child",
                        "identifier": "OMN-999",
                        "state": {"name": "In Progress"},
                    }
                ],
                "pageInfo": {"hasNextPage": False},
            }
        }
    }
    result = await HandlerLinearTriage(
        client=linear,
        github_client=_github_with_merged_implementing_pr(),
        dod_verdict_probe=_Probe(_BOUND),
    ).handle(ModelLinearTriageStartCommand(project_id="sprint-uuid", flag_only=False))
    assert result.marked_done == 0
    linear.save_issue.assert_not_called()


@pytest.mark.unit
async def test_explicit_backlog_scope_ignores_sprint_config(monkeypatch):
    monkeypatch.setenv("LINEAR_ACTIVE_SPRINT_PROJECT_ID", "configured-sprint")
    linear, github = _clients()
    result = await HandlerLinearTriage(client=linear, github_client=github).handle(
        ModelLinearTriageStartCommand(scope="backlog")
    )
    assert result.status == "completed"
    assert linear.list_issues.call_args.kwargs["project_id"] is None


@pytest.mark.unit
def test_backlog_scope_cannot_silently_ignore_an_explicit_project():
    from pydantic import ValidationError

    with pytest.raises(ValidationError, match="backlog scope"):
        ModelLinearTriageStartCommand(scope="backlog", project_id="sprint-uuid")


@pytest.mark.unit
async def test_missing_explicit_mapping_cannot_select_ambient_inline_config(
    tmp_path, monkeypatch
):
    import json

    monkeypatch.setenv(
        "ONEX_SECRET_RESOLVER_CONFIG_JSON",
        json.dumps({"enable_convention_fallback": True}),
    )
    monkeypatch.setenv("LINEAR_API_KEY", "ambient-key")
    monkeypatch.setenv("GITHUB_TOKEN", "ambient-key")
    from omnimarket.nodes.node_linear_triage.handlers import (
        handler_linear_triage as module,
    )

    linear, github = _clients()
    monkeypatch.setattr(module, "LinearHttpClient", MagicMock(return_value=linear))
    monkeypatch.setattr(module, "GitHubHttpClient", MagicMock(return_value=github))
    result = await HandlerLinearTriage().handle(
        ModelLinearTriageStartCommand(
            project_id="sprint-uuid",
            secret_resolver_config_path=str(tmp_path / "missing.yaml"),
        )
    )
    assert result.status == "error"
    linear.list_issues.assert_not_called()
