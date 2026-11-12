# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Compose named endpoint discovery, canonical credentials and routing ownership."""

import asyncio
import fcntl
import os
import tempfile
from pathlib import Path

import yaml
from pydantic import SecretStr

from omnimarket.handlers.delegation_first_choice import first_choice_path
from omnimarket.inference.local_byok_credential_adapter import (
    LocalByokCredentialStore,
)
from omnimarket.models.delegation.wire.model_bifrost_delegation_config import (
    ModelDelegationBackendConfig,
)
from omnimarket.models.model_endpoint_models import (
    ModelEndpointModelsRequest,
)
from omnimarket.nodes.node_model_setup_effect.connection_storage import (
    read_named_connections,
    register_named_connection,
    remove_named_connection,
)
from omnimarket.nodes.node_model_setup_effect.handlers.handler_endpoint_models import (
    HandlerEndpointModels,
)
from omnimarket.nodes.node_model_setup_effect.handlers.handler_model_setup import (
    HandlerModelSetup,
)
from omnimarket.nodes.node_model_setup_effect.models.model_endpoint_connection import (
    ModelEndpointConnection,
    ModelEndpointConnectionRequest,
    ModelEndpointConnectionResult,
)
from omnimarket.projection.sqlite_database import default_evidence_db_path


class HandlerEndpointConnections:
    """Connection mutations share a nonblocking lock and preserve unrelated routes."""

    def __init__(self, *, db_path: Path | None = None) -> None:
        self.db_path = db_path if db_path is not None else default_evidence_db_path()
        self.overlay_path = self.db_path.parent / "bifrost_overrides.yaml"

    def _connections(self) -> tuple[ModelEndpointConnection, ...]:
        return tuple(
            ModelEndpointConnection.model_validate(row)
            for row in read_named_connections(db_path=self.db_path)
        )

    def handle(
        self, request: ModelEndpointConnectionRequest
    ) -> ModelEndpointConnectionResult:
        if request.operation == "list":
            connections = self._connections()
            if request.connection_id:
                connections = tuple(
                    c for c in connections if c.connection_id == request.connection_id
                )
            return ModelEndpointConnectionResult(
                status="ready",
                connections=connections,
                tests=tuple(
                    test
                    for connection in connections
                    for test in HandlerModelSetup.connection_tests(
                        connection.connection_id, db_path=self.db_path
                    )
                ),
            )
        if request.connection_id is None:
            return ModelEndpointConnectionResult(
                status="refused", message="Name the connection."
            )
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with (self.db_path.parent / "connections.lock").open("a+") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return ModelEndpointConnectionResult(
                    status="refused",
                    message="Another model connection operation is running. Retry after it finishes.",
                )
            try:
                return self._handle_locked(request)
            except (OSError, ValueError, yaml.YAMLError):
                return ModelEndpointConnectionResult(
                    status="refused",
                    message="The connection could not be reconciled. Check its endpoint, credential and existing configuration.",
                )
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)

    def _handle_locked(
        self, request: ModelEndpointConnectionRequest
    ) -> ModelEndpointConnectionResult:
        connections = self._connections()
        existing = next(
            (c for c in connections if c.connection_id == request.connection_id), None
        )
        if request.operation != "connect" and existing is None:
            return ModelEndpointConnectionResult(
                status="refused", message="This connection is not configured."
            )
        if request.operation == "remove" and existing is not None:
            self._routes(existing, remove=True)
            remove_named_connection(existing.connection_id, db_path=self.db_path)
            self._clear_selection_and_proofs(existing)
            return ModelEndpointConnectionResult(
                status="ready", connections=self._connections()
            )
        if request.operation == "select" and existing is not None:
            models = request.models or existing.models[:1]
            if len(models) != 1 or models[0] not in existing.models:
                return ModelEndpointConnectionResult(
                    status="refused",
                    message="Select one model belonging to this connection.",
                )
            import json

            self._atomic(
                first_choice_path(self.db_path),
                json.dumps(
                    {
                        "provider": "connection:" + existing.connection_id,
                        "model": models[0],
                    },
                    indent=2,
                ),
            )
            return ModelEndpointConnectionResult(
                status="ready", connections=(existing,)
            )
        if request.operation == "test" and existing is not None:
            return self._test(existing)
        url = request.url or (existing.url if existing else None)
        if url is None:
            return ModelEndpointConnectionResult(
                status="refused",
                message="Supply the already-running server's endpoint URL.",
            )
        if (
            existing
            and existing.secret_ref
            and request.key is None
            and existing.url != url
        ):
            return ModelEndpointConnectionResult(
                status="refused",
                message="A changed endpoint requires its own newly supplied credential.",
            )
        key = request.key
        previous_key = None
        if existing and existing.secret_ref:
            previous_key = asyncio.run(
                LocalByokCredentialStore(db_path=self.db_path).get_secret(
                    existing.secret_ref
                )
            )
        if key is None and existing and existing.secret_ref:
            value = previous_key
            if value is None:
                return ModelEndpointConnectionResult(
                    status="refused",
                    message="The connection credential is missing; supply it again.",
                )
            key = SecretStr(value)
        catalog = HandlerEndpointModels().handle(
            ModelEndpointModelsRequest(url=url, key=key)
        )
        if catalog.status not in ("ready", "unsupported"):
            return ModelEndpointConnectionResult(
                status="refused", message=catalog.message
            )
        if request.operation == "refresh":
            return ModelEndpointConnectionResult(
                status="ready",
                connections=(existing,) if existing else (),
                catalog=catalog.models,
                message=catalog.message,
            )
        models = request.models or (existing.models if existing else ())
        if not models:
            return ModelEndpointConnectionResult(
                status="refused",
                catalog=catalog.models,
                message="Select ordered model IDs from this endpoint before connecting.",
            )
        if catalog.status == "ready" and any(
            model not in catalog.models for model in models
        ):
            return ModelEndpointConnectionResult(
                status="refused",
                catalog=catalog.models,
                message="A selected model is absent from this endpoint's current catalog.",
            )
        # Validate the target overlay before storing any key.
        self._check_ownership(existing, request.connection_id or "", models)
        provider = "openai_compatible" if key else "local"
        register_named_connection(
            request.connection_id or "",
            provider,
            url,
            models,
            key.get_secret_value() if key else None,
            db_path=self.db_path,
            native_messages=request.native_messages,
        )
        if existing and (
            existing.url != url
            or existing.models != models
            or previous_key != (key.get_secret_value() if key else None)
        ):
            self._clear_proofs(existing)
        current = next(
            c for c in self._connections() if c.connection_id == request.connection_id
        )
        self._routes(current, previous=existing)
        if request.prove:
            return self._test(current)
        return ModelEndpointConnectionResult(
            status="ready",
            connections=(current,),
            catalog=catalog.models,
            message="Configured; inference has not been tested.",
        )

    def _test(
        self, connection: ModelEndpointConnection
    ) -> ModelEndpointConnectionResult:
        owner = HandlerModelSetup()
        tests = tuple(
            owner.test_backend(
                connection.connection_id,
                backend,
                model,
                connection.url + "/v1/chat/completions",
                db_path=self.db_path,
            )
            for backend, model in zip(
                connection.backend_ids, connection.models, strict=True
            )
        )
        return ModelEndpointConnectionResult(
            status="ready" if all(t.status == "passed" for t in tests) else "failed",
            connections=(connection,),
            tests=tests,
            message=None
            if all(t.status == "passed" for t in tests)
            else "A selected model failed its pinned delegation. Retry with models test-connection.",
        )

    def _overlay(self) -> dict[str, object]:
        if not self.overlay_path.exists():
            return {}
        document = yaml.safe_load(self.overlay_path.read_text())
        if not isinstance(document, dict) or not isinstance(
            document.get("backends", []), list
        ):
            raise ValueError("Invalid routing overlay")
        return document

    @staticmethod
    def _backend_rows(connection: ModelEndpointConnection) -> list[dict[str, object]]:
        rows = []
        for model in connection.models:
            for native in (False, True) if connection.native_messages else (False,):
                backend = ModelDelegationBackendConfig(
                    backend_id=connection.backend_id(model, native=native),
                    provider=connection.provider,
                    endpoint_url=connection.url
                    + ("/v1/messages" if native else "/v1/chat/completions"),
                    model_name=model,
                    secret_ref=connection.secret_ref,
                    tier="cheap_cloud" if connection.secret_ref else "local",
                    explicit_pin_only=native,
                )
                rows.append(
                    {
                        **backend.model_dump(mode="json", exclude_none=True),
                        "secret_ref": connection.secret_ref,
                    }
                )
        return rows

    def _check_ownership(
        self,
        previous: ModelEndpointConnection | None,
        connection_id: str,
        models: tuple[str, ...],
    ) -> None:
        document = self._overlay()
        expected = (
            {row["backend_id"]: row for row in self._backend_rows(previous)}
            if previous
            else {}
        )
        candidate = ModelEndpointConnection(
            connection_id=connection_id, provider="local", url="", models=models
        )
        reserved = {
            *candidate.backend_ids,
            *(candidate.backend_id(model, native=True) for model in models),
            *expected,
        }
        rows = document.get("backends", [])
        if not isinstance(rows, list):
            raise ValueError("Invalid routing overlay")
        seen = set()
        for row in rows:
            if not isinstance(row, dict):
                raise ValueError("Invalid routing backend")
            backend = row.get("backend_id")
            if backend in reserved:
                if (
                    backend in seen
                    or backend not in expected
                    or row != expected[backend]
                ):
                    raise ValueError(
                        "Connection routing was edited by another owner; preserve and review it"
                    )
                seen.add(backend)

    def _routes(
        self,
        connection: ModelEndpointConnection,
        *,
        previous: ModelEndpointConnection | None = None,
        remove: bool = False,
    ) -> None:
        self._check_ownership(
            previous or connection, connection.connection_id, connection.models
        )
        document = self._overlay()
        old = previous or connection
        owned = {
            *old.backend_ids,
            *(old.backend_id(m, native=True) for m in old.models),
        }
        rows = document.get("backends", [])
        if not isinstance(rows, list):
            raise ValueError("Invalid routing overlay")
        kept = [
            row
            for row in rows
            if not (isinstance(row, dict) and row.get("backend_id") in owned)
        ]
        if not remove:
            kept.extend(self._backend_rows(connection))
        self._atomic(
            self.overlay_path,
            yaml.safe_dump({**document, "backends": kept}, sort_keys=False),
        )

    def _clear_selection_and_proofs(self, connection: ModelEndpointConnection) -> None:
        import json

        choice = first_choice_path(self.db_path)
        if choice.is_file():
            document = json.loads(choice.read_text())
            if document.get("provider") == "connection:" + connection.connection_id:
                self._atomic(choice, "{}")
        self._clear_proofs(connection)

    def _clear_proofs(self, connection: ModelEndpointConnection) -> None:
        import json

        from omnimarket.inference.local_byok_credential_adapter import (
            local_model_tests_path,
        )

        proofs = local_model_tests_path(self.db_path)
        if proofs.is_file():
            document = json.loads(proofs.read_text())
            if document.pop("connection:" + connection.connection_id, None) is not None:
                self._atomic(proofs, json.dumps(document, indent=2, sort_keys=True))

    @staticmethod
    def _atomic(path: Path, content: str) -> None:
        if path.is_file() and path.read_text() == content:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix=".connection-", dir=path.parent)
        try:
            with os.fdopen(fd, "w") as stream:
                stream.write(content)
            os.replace(temporary, path)
        finally:
            Path(temporary).unlink(missing_ok=True)
