# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Setup connection identity and proof controls precede integration changes."""

import json
from pathlib import Path

import pytest
from click.testing import CliRunner
from pydantic import ValidationError

from omnimarket.nodes.node_onex_setup_effect.models.model_setup_plan import (
    ModelSetupPlan,
)


@pytest.mark.parametrize(
    "args",
    [
        ["--provider", "local", "--model", "a"],
        ["--provider", "local", "--url", "http://host"],
    ],
)
def test_json_argument_failure_is_typed(tmp_path: Path, args: list[str]) -> None:
    from omnimarket.nodes.node_onex_setup_effect.cli import setup_command

    result = CliRunner().invoke(
        setup_command,
        ["--workspace", str(tmp_path), "--json", "--no-interactive", "--yes", *args],
    )
    assert result.exit_code != 0
    body = json.loads(result.stdout)
    assert body["status"] == "refused"
    assert body["error_code"] == "invalid_setup_selection"
    assert body["message"]


def test_same_provider_connections_have_independent_credential_owners(
    tmp_path: Path,
) -> None:
    selections = [
        {
            "provider": "openai_compatible",
            "connection_id": name,
            "url": f"https://{name}.example",
            "models": ["model"],
        }
        for name in ("first", "second")
    ]
    plan = ModelSetupPlan(workspace=tmp_path, providers=selections)
    assert [p.credential_identity for p in plan.providers] == [
        "connection:first",
        "connection:second",
    ]
    with pytest.raises(ValidationError):
        ModelSetupPlan(workspace=tmp_path, providers=[selections[0], selections[0]])


def test_setup_cli_connection_routes_secret_to_named_owner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from omnimarket.nodes.node_onex_setup_effect import cli
    from omnimarket.nodes.node_onex_setup_effect.models.model_setup_journal import (
        ModelSetupJournal,
    )

    captured = {}

    class Workflow:
        @staticmethod
        def saved_plan(_home):
            return None

        @staticmethod
        def supported_components():
            return {"delegation"}

        def __init__(self, **kwargs):
            captured.update(kwargs)

        def handle(self, plan):
            captured["plan"] = plan
            return ModelSetupJournal(
                workspace=plan.workspace,
                install_path="local",
                desired_digest="proof",
                plan=plan,
            )

    monkeypatch.setattr(cli, "HandlerSetupWorkflow", Workflow)
    monkeypatch.setattr(cli, "user_config_path", lambda: tmp_path / "config.yaml")
    result = CliRunner().invoke(
        cli.setup_command,
        [
            "--workspace",
            str(tmp_path / "workspace"),
            "--connection",
            "mine",
            "--provider",
            "ollama",
            "--endpoint-auth",
            "api-key",
            "--url",
            "http://127.0.0.1:9999",
            "--model",
            "dynamic-model",
            "--yes",
            "--no-interactive",
            "--json",
        ],
        input="private-key-sentinel",
    )
    assert result.exit_code == 0, result.output
    assert captured["plan"].providers[0].connection_id == "mine"
    assert (
        captured["credentials"]["connection:mine"].get_secret_value()
        == "private-key-sentinel"
    )
    assert "private-key-sentinel" not in result.output


def test_named_observation_requires_every_model_proof_and_does_not_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from omnimarket.handlers.handler_endpoint_connections import HandlerModelConnections
    from omnimarket.inference.local_byok_credential_adapter import (
        register_named_connection,
    )
    from omnimarket.nodes.node_model_setup_effect.models.model_endpoint_connection import (
        ModelEndpointConnection,
    )
    from omnimarket.nodes.node_onex_setup_effect.handlers import (
        handler_setup_environment as environment,
    )

    db = tmp_path / "store.sqlite"
    register_named_connection(
        "mine", "local", "http://127.0.0.1:9999", ("a", "b"), None, db_path=db
    )
    monkeypatch.setattr(
        environment,
        "HandlerModelConnections",
        lambda: HandlerModelConnections(db_path=db),
    )
    plan = ModelSetupPlan(
        workspace=tmp_path,
        providers=[
            {
                "provider": "ollama",
                "connection_id": "mine",
                "url": "http://127.0.0.1:9999",
                "models": ["a", "b"],
            }
        ],
    )
    connection = ModelEndpointConnection(
        connection_id="mine",
        provider="local",
        url="http://127.0.0.1:9999",
        models=("a", "b"),
    )
    rows = [
        {
            "provider": "local",
            "status": "passed",
            "tested_at": "2026-11-11T00:00:00Z",
            "backend_id": connection.backend_id(model),
            "model": model,
            "endpoint": connection.url + "/v1/chat/completions",
        }
        for model in ("a", "b")
    ]
    path = tmp_path / "model_tests.json"
    path.write_text(json.dumps({"connection:mine": rows[:1]}))
    handler = environment.HandlerSetupEnvironment(name="models", onex_home=tmp_path)
    assert handler.observe(plan).status != "success"
    path.write_text(json.dumps({"connection:mine": rows}))
    before = {
        str(p): (p.read_bytes(), p.stat().st_mtime_ns)
        for p in tmp_path.iterdir()
        if p.is_file()
    }
    result = handler.observe(plan)
    assert result.status == "success"
    assert len(result.receipts) == 2
    assert before == {
        str(p): (p.read_bytes(), p.stat().st_mtime_ns)
        for p in tmp_path.iterdir()
        if p.is_file()
    }


def test_setup_cli_local_stack_selection_is_explicit(tmp_path, monkeypatch):
    from omnimarket.nodes.node_onex_setup_effect import cli
    from omnimarket.nodes.node_onex_setup_effect.models.model_setup_journal import (
        ModelSetupJournal,
    )

    captured = {}

    class Workflow:
        @staticmethod
        def saved_plan(_home):
            return None

        @staticmethod
        def supported_components():
            return {"local-stack"}

        def __init__(self, **kwargs):
            pass

        def handle(self, plan):
            captured["plan"] = plan
            return ModelSetupJournal(
                workspace=plan.workspace,
                install_path="local",
                desired_digest="proof",
                plan=plan,
            )

    monkeypatch.setattr(cli, "HandlerSetupWorkflow", Workflow)
    monkeypatch.setattr(cli, "user_config_path", lambda: tmp_path / "config.yaml")
    args = [
        "--workspace",
        str(tmp_path),
        "--component",
        "local-stack",
        "--compose-file",
        str(tmp_path / "compose.yaml"),
        "--topology-file",
        str(tmp_path / "topology.yaml"),
        "--json",
        "--no-interactive",
        "--yes",
    ]
    result = CliRunner().invoke(cli.setup_command, args)
    assert result.exit_code == 0, result.output
    assert captured["plan"].local_stack.compose_file == tmp_path / "compose.yaml"
    assert captured["plan"].local_stack.topology_file == tmp_path / "topology.yaml"
    assert captured["plan"].providers == ()


@pytest.mark.parametrize("provider", ["anthropic", "glm"])
def test_setup_accepts_canonical_provider_catalogue(tmp_path, provider):
    from omnimarket.handlers.routing.byok_provider_backends import (
        customer_provider_catalogue,
    )

    assert provider in customer_provider_catalogue()
    plan = ModelSetupPlan(
        workspace=tmp_path,
        providers=[{"provider": provider, "models": ["selected-model"]}],
    )
    assert plan.providers[0].registration_provider == provider


@pytest.mark.parametrize("changed", ["credential", "default"])
def test_setup_observe_does_not_skip_requested_connection_change(
    tmp_path, monkeypatch, changed
):
    from pydantic import SecretStr

    from omnimarket.handlers.handler_endpoint_connections import HandlerModelConnections
    from omnimarket.inference import local_byok_credential_adapter as credential_owner
    from omnimarket.nodes.node_model_setup_effect.models.model_endpoint_connection import (
        ModelEndpointConnection,
    )
    from omnimarket.nodes.node_onex_setup_effect.handlers import (
        handler_setup_environment as environment,
    )

    db = tmp_path / "store.sqlite"
    monkeypatch.setattr(credential_owner, "default_evidence_db_path", lambda: db)
    credential_owner.register_named_connection(
        "mine",
        "openai_compatible",
        "https://server.example",
        ("m",),
        "old-key",
        db_path=db,
    )
    connection = ModelEndpointConnection.model_validate(
        credential_owner.read_named_connections(db_path=db)[0]
    )
    monkeypatch.setattr(
        environment,
        "HandlerModelConnections",
        lambda: HandlerModelConnections(db_path=db),
    )
    monkeypatch.setattr(
        environment, "read_first_choice", lambda: ("connection:other", "m")
    )
    (tmp_path / "model_tests.json").write_text(
        json.dumps(
            {
                "connection:mine": [
                    {
                        "provider": "openai_compatible",
                        "status": "passed",
                        "tested_at": "2026-11-11T00:00:00Z",
                        "backend_id": connection.backend_id("m"),
                        "model": "m",
                        "endpoint": connection.url + "/v1/chat/completions",
                    }
                ]
            }
        )
    )
    plan = ModelSetupPlan(
        workspace=tmp_path,
        providers=[
            {
                "provider": "openai_compatible",
                "connection_id": "mine",
                "url": connection.url,
                "models": ["m"],
                "make_default": changed == "default",
            }
        ],
    )
    handler = environment.HandlerSetupEnvironment(
        name="models",
        onex_home=tmp_path,
        credentials={"connection:mine": SecretStr("new-key")}
        if changed == "credential"
        else {},
    )
    assert handler.observe(plan).status != "success"
