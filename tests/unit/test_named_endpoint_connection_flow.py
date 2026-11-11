# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Failure-first connection flow: cross-server keys, stale choices and owned routing.

HTTP listing is real loopback traffic. Inference is injected here; the customer
journey must separately prove real inference. The artifact records each server's
auth verdict, backend references and refusal, never key values.
"""

import asyncio
import json
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread

import pytest
import yaml
from pydantic import SecretStr

from omnimarket.inference.local_byok_credential_adapter import LocalByokCredentialStore


@contextmanager
def endpoint(key: str | list[str], models: list[str]):
    accepted = []

    class Server(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_GET(self):
            valid = self.headers.get("Authorization") == "Bearer " + (
                key[0] if isinstance(key, list) else key
            )
            accepted.append(valid)
            self.send_response(200 if valid else 401)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(
                json.dumps({"data": [{"id": name} for name in models]}).encode()
            )

    server = ThreadingHTTPServer(("127.0.0.1", 0), Server)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", accepted
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


def test_connection_discovery_isolated_and_owned_routes_removed(tmp_path: Path) -> None:
    from omnimarket.nodes.node_model_setup_effect.handlers.handler_endpoint_connections import (
        HandlerEndpointConnections,
    )
    from omnimarket.nodes.node_model_setup_effect.models.model_endpoint_connection import (
        ModelEndpointConnectionRequest,
    )

    db = tmp_path / "credentials.sqlite"
    handler = HandlerEndpointConnections(db_path=db)
    overlay = tmp_path / "bifrost_overrides.yaml"
    overlay.write_text(
        "backends:\n  - backend_id: unrelated\n    provider: local\n    endpoint_url: http://other/v1/chat/completions\n    model_name: other\n    tier: local\n"
    )
    with (
        endpoint("first-secret", ["new-one", "backup"]) as (one, seen_one),
        endpoint("second-secret", ["new-two"]) as (two, seen_two),
    ):
        for name, url, key, models in (
            ("one", one, "first-secret", ("new-one", "backup")),
            ("two", two, "second-secret", ("new-two",)),
        ):
            result = handler.handle(
                ModelEndpointConnectionRequest(
                    operation="connect",
                    connection_id=name,
                    url=url,
                    models=models,
                    key=SecretStr(key),
                    prove=False,
                )
            )
            assert result.status == "ready"
        rows = handler.handle(
            ModelEndpointConnectionRequest(operation="list")
        ).connections
        assert len(rows) == 2
        backends = yaml.safe_load(overlay.read_text())["backends"]
        assert len(backends) == 4
        store = LocalByokCredentialStore(db_path=db)
        for connection, expected in zip(
            rows, ("first-secret", "second-secret"), strict=True
        ):
            owned = [b for b in backends if b["backend_id"] in connection.backend_ids]
            assert len(owned) == len(connection.models)
            assert {b["endpoint_url"] for b in owned} == {
                connection.url + "/v1/chat/completions"
            }
            assert {asyncio.run(store.get_secret(b["secret_ref"])) for b in owned} == {
                expected
            }
        before = overlay.read_bytes()
        refused = handler.handle(
            ModelEndpointConnectionRequest(
                operation="connect",
                connection_id="one",
                url=two,
                models=("new-two",),
                prove=False,
            )
        )
        assert refused.status == "refused"
        assert seen_two == [True], "An omitted key was forwarded to a different server"
        assert overlay.read_bytes() == before
        bad = handler.handle(
            ModelEndpointConnectionRequest(
                operation="connect",
                connection_id="bad",
                url=one,
                models=("new-one",),
                key=SecretStr("wrong-key"),
                prove=False,
            )
        )
        assert bad.status == "refused"
        assert overlay.read_bytes() == before
        removed = handler.handle(
            ModelEndpointConnectionRequest(operation="remove", connection_id="one")
        )
        assert removed.status == "ready"
        kept = yaml.safe_load(overlay.read_text())["backends"]
        assert kept[0]["backend_id"] == "unrelated"
        assert len(kept) == 2
        assert all(b["backend_id"] not in rows[0].backend_ids for b in kept)
        artifact = {
            "server_one_auth": seen_one,
            "server_two_auth": seen_two,
            "connections": [r.model_dump(mode="json") for r in rows],
            "refused": refused.model_dump(mode="json"),
            "remaining_backend_ids": [b["backend_id"] for b in kept],
        }
        serialized = json.dumps(artifact, indent=2)
        assert "first-secret" not in serialized
        assert "second-secret" not in serialized
        (tmp_path / "named-connections-evidence.json").write_text(serialized)


@pytest.mark.parametrize(
    "url", ["http://host bad", "http://user:secret@host", "http://host?key=secret"]
)
def test_connection_rejects_unsafe_urls(url: str) -> None:
    from omnimarket.nodes.node_model_setup_effect.models.model_endpoint_connection import (
        ModelEndpointConnectionRequest,
    )

    with pytest.raises(ValueError, match="url"):
        ModelEndpointConnectionRequest(
            operation="connect", connection_id="one", url=url, models=("a",)
        )


def test_named_proof_checks_endpoint_and_status_observation_is_readonly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from omnimarket.nodes.node_model_setup_effect.handlers import (
        handler_model_setup as owner,
    )
    from omnimarket.nodes.node_model_setup_effect.handlers.handler_endpoint_connections import (
        HandlerEndpointConnections,
    )
    from omnimarket.nodes.node_model_setup_effect.models.model_endpoint_connection import (
        ModelEndpointConnectionRequest,
    )

    handler = HandlerEndpointConnections(db_path=tmp_path / "credentials.sqlite")
    wrong = False
    receipt = tmp_path / "receipt.json"
    with endpoint("key", ["model"]) as (url, _seen):

        def run(backend_id, **_kwargs):
            receipt.write_text(
                json.dumps(
                    {
                        "status": "success",
                        "backend_id": backend_id,
                        "model": "model",
                        "endpoint": "http://wrong/v1/chat/completions"
                        if wrong
                        else url + "/v1/chat/completions",
                    }
                )
            )
            return 0, "", str(receipt)

        monkeypatch.setattr(owner, "run_pinned_delegation", run)
        passed = handler.handle(
            ModelEndpointConnectionRequest(
                operation="connect",
                connection_id="one",
                url=url,
                models=("model",),
                key=SecretStr("key"),
            )
        )
        assert passed.status == "ready"
        assert passed.tests[0].status == "passed"
        snapshot = {
            str(p): (p.read_bytes(), p.stat().st_mtime_ns)
            for p in tmp_path.rglob("*")
            if p.is_file()
        }
        status = handler.handle(ModelEndpointConnectionRequest(operation="list"))
        assert status.tests == passed.tests
        assert snapshot == {
            str(p): (p.read_bytes(), p.stat().st_mtime_ns)
            for p in tmp_path.rglob("*")
            if p.is_file()
        }
        wrong = True
        failed = handler.handle(
            ModelEndpointConnectionRequest(operation="test", connection_id="one")
        )
        assert failed.status == "failed"
        assert failed.tests[0].status == "failed"
        assert "endpoint" in failed.tests[0].reason


def test_native_declaration_survives_reconnect_and_user_route_edit_is_preserved(
    tmp_path: Path,
) -> None:
    from omnimarket.nodes.node_model_setup_effect.handlers.handler_endpoint_connections import (
        HandlerEndpointConnections,
    )
    from omnimarket.nodes.node_model_setup_effect.models.model_endpoint_connection import (
        ModelEndpointConnectionRequest,
    )

    handler = HandlerEndpointConnections(db_path=tmp_path / "credentials.sqlite")
    with endpoint("key", ["model"]) as (url, _seen):
        request = ModelEndpointConnectionRequest(
            operation="connect",
            connection_id="one",
            url=url,
            models=("model",),
            key=SecretStr("key"),
            native_messages=True,
            prove=False,
        )
        assert handler.handle(request).status == "ready"
        before = handler.overlay_path.read_bytes()
        repeated = handler.handle(
            request.model_copy(update={"native_messages": None, "key": None})
        )
        assert repeated.status == "ready"
        assert repeated.connections[0].native_messages is True
        assert handler.overlay_path.read_bytes() == before
        document = yaml.safe_load(before)
        document["backends"][0]["max_tokens"] = 16383
        handler.overlay_path.write_text(yaml.safe_dump(document))
        edited = handler.overlay_path.read_bytes()
        refused = handler.handle(
            ModelEndpointConnectionRequest(operation="remove", connection_id="one")
        )
        assert refused.status == "refused"
        assert handler.overlay_path.read_bytes() == edited
        assert (
            len(
                handler.handle(
                    ModelEndpointConnectionRequest(operation="list")
                ).connections
            )
            == 1
        )


def test_default_choice_resolves_exact_named_backend_and_remove_clears_only_own_choice(
    tmp_path: Path,
) -> None:
    from omnimarket.handlers.delegation_first_choice import (
        first_choice_path,
        first_choice_route,
    )
    from omnimarket.nodes.node_model_setup_effect.handlers.handler_endpoint_connections import (
        HandlerEndpointConnections,
    )
    from omnimarket.nodes.node_model_setup_effect.models.model_endpoint_connection import (
        ModelEndpointConnectionRequest,
    )

    db = tmp_path / "credentials.sqlite"
    handler = HandlerEndpointConnections(db_path=db)
    with endpoint("key", ["model"]) as (url, _seen):
        configured = handler.handle(
            ModelEndpointConnectionRequest(
                operation="connect",
                connection_id="one",
                url=url,
                models=("model",),
                key=SecretStr("key"),
                prove=False,
            )
        )
        assert configured.status == "ready"
        assert (
            handler.handle(
                ModelEndpointConnectionRequest(operation="select", connection_id="one")
            ).status
            == "ready"
        )
        route = first_choice_route("document", db_path=db)
        assert route is not None
        assert route.backend_id == configured.connections[0].backend_ids[0]
        assert route.endpoint_ref == url + "/v1/chat/completions"
        assert route.secret_ref == configured.connections[0].secret_ref
        assert (
            handler.handle(
                ModelEndpointConnectionRequest(operation="remove", connection_id="one")
            ).status
            == "ready"
        )
        assert json.loads(first_choice_path(db).read_text()) == {}


def test_rotated_key_invalidates_old_passing_inference_proof(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from omnimarket.nodes.node_model_setup_effect.handlers import (
        handler_model_setup as owner,
    )
    from omnimarket.nodes.node_model_setup_effect.handlers.handler_endpoint_connections import (
        HandlerEndpointConnections,
    )
    from omnimarket.nodes.node_model_setup_effect.models.model_endpoint_connection import (
        ModelEndpointConnectionRequest,
    )

    key = ["old-key"]
    handler = HandlerEndpointConnections(db_path=tmp_path / "store.sqlite")
    receipt = tmp_path / "receipt.json"
    with endpoint(key, ["model"]) as (url, _seen):

        def run(backend_id, **_kwargs):
            receipt.write_text(
                json.dumps(
                    {
                        "status": "success",
                        "backend_id": backend_id,
                        "model": "model",
                        "endpoint": url + "/v1/chat/completions",
                    }
                )
            )
            return 0, "", str(receipt)

        monkeypatch.setattr(owner, "run_pinned_delegation", run)
        first = handler.handle(
            ModelEndpointConnectionRequest(
                operation="connect",
                connection_id="one",
                url=url,
                models=("model",),
                key=SecretStr(key[0]),
            )
        )
        assert first.tests[0].status == "passed"
        key[0] = "new-key"
        assert (
            handler.handle(
                ModelEndpointConnectionRequest(
                    operation="connect",
                    connection_id="one",
                    url=url,
                    models=("model",),
                    key=SecretStr(key[0]),
                    prove=False,
                )
            ).status
            == "ready"
        )
        assert (
            handler.handle(ModelEndpointConnectionRequest(operation="list")).tests == ()
        )


def test_selected_named_models_order_and_removed_model_cannot_route(tmp_path):
    from omnimarket.handlers.delegation_first_choice import first_choice_routes
    from omnimarket.nodes.node_model_setup_effect.handlers.handler_endpoint_connections import (
        HandlerEndpointConnections,
    )
    from omnimarket.nodes.node_model_setup_effect.models.model_endpoint_connection import (
        ModelEndpointConnectionRequest,
    )

    db = tmp_path / "store.sqlite"
    owner = HandlerEndpointConnections(db_path=db)
    with endpoint("key", ["a", "b", "c"]) as (url, _):
        assert (
            owner.handle(
                ModelEndpointConnectionRequest(
                    operation="connect",
                    connection_id="mine",
                    url=url,
                    models=("b", "a", "c"),
                    key=SecretStr("key"),
                    prove=False,
                )
            ).status
            == "ready"
        )
        assert (
            owner.handle(
                ModelEndpointConnectionRequest(
                    operation="select", connection_id="mine", models=("b",)
                )
            ).status
            == "ready"
        )
        assert [r.model_id for r in first_choice_routes("document", db_path=db)] == [
            "b",
            "a",
            "c",
        ]
        assert (
            owner.handle(
                ModelEndpointConnectionRequest(
                    operation="connect",
                    connection_id="mine",
                    url=url,
                    models=("b", "c"),
                    prove=False,
                )
            ).status
            == "ready"
        )
        assert [r.model_id for r in first_choice_routes("document", db_path=db)] == [
            "b",
            "c",
        ]
