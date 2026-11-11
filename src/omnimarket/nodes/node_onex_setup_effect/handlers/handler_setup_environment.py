# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Reconcile native setup using the surviving config, identity and model owners.

The workflow alone writes setup.json. Runtime configuration reuses the original
model-PR implementation with its legacy state writer explicitly disabled.
"""

from __future__ import annotations

import asyncio
import os
import stat
from pathlib import Path
from typing import Literal

from omnibase_core.cli.cli_user_config import read_user_config, user_config_path
from pydantic import SecretStr

from omnimarket.handlers import handler_model_keys as model_keys
from omnimarket.handlers.delegation_first_choice import read_first_choice
from omnimarket.handlers.handler_endpoint_connections import (
    HandlerModelConnections,
    ModelEndpointConnectionRequest,
)
from omnimarket.inference.local_byok_credential_adapter import LocalByokCredentialStore
from omnimarket.local_deployment.tenant_identity import (
    mint_local_tenant_identity,
    read_local_tenant_identity,
)
from omnimarket.nodes.node_onex_setup_effect.handlers import (
    handler_onex_setup as original,
)
from omnimarket.nodes.node_onex_setup_effect.models.model_onex_setup import (
    ModelOnexSetupReceipt,
    ModelOnexSetupRequest,
)
from omnimarket.nodes.node_onex_setup_effect.models.model_setup_observation import (
    ModelSetupObservation,
)
from omnimarket.nodes.node_onex_setup_effect.models.model_setup_plan import (
    ModelSetupPlan,
)
from omnimarket.nodes.node_onex_setup_effect.models.model_setup_step_request import (
    ModelSetupStepRequest,
)

_Step = Literal["workspace", "user_config", "runtime_config", "identity", "models"]


def _read(path: Path) -> str | None:
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return None
    except OSError:
        raise ValueError("Expected a bounded regular setup file") from None
    with os.fdopen(fd, "rb") as source:
        info = os.fstat(source.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > 1024 * 1024:
            raise ValueError("Expected a bounded regular setup file")
        raw = source.read(1024 * 1024 + 1)
    if len(raw) > 1024 * 1024:
        raise ValueError("Expected a bounded regular setup file")
    return raw.decode()


class HandlerSetupEnvironment:
    """One native component adapter; all observations are read-only."""

    def __init__(
        self,
        *,
        name: _Step = "workspace",
        onex_home: Path | None = None,
        credentials: dict[str, SecretStr] | None = None,
        model_step: original.ModelStep | None = None,
    ) -> None:
        self.name = name
        self._home = onex_home or user_config_path().parent
        self._credentials = credentials or {}
        self._prove_model = model_step or original.prove_model

    def handle(self, request: ModelSetupStepRequest) -> ModelSetupObservation:
        adapter = HandlerSetupEnvironment(
            name=request.step,
            onex_home=self._home,
            credentials=self._credentials,
            model_step=self._prove_model,
        )
        if request.operation == "apply":
            result = adapter.apply(request.plan)
            if result is not None and result.status not in ("success", "skipped"):
                return result
        return adapter.observe(request.plan)

    def _result(
        self,
        ready: bool,
        *,
        evidence: dict[str, str] | None = None,
        fix: str | None = None,
    ) -> ModelSetupObservation:
        return ModelSetupObservation(
            step=self.name,
            status="success" if ready else "unavailable",
            evidence=evidence or {},
            fix=None
            if ready
            else fix or "Run onex setup --action repair for this component.",
        )

    def observe(self, plan: ModelSetupPlan) -> ModelSetupObservation:
        if self.name == "workspace":
            ready = (
                plan.workspace.is_dir()
                and _read(plan.workspace / ".onex-setup-workspace") == "onex setup\n"
            )
            if plan.workspace.exists() and not plan.workspace.is_dir():
                return ModelSetupObservation(
                    step=self.name,
                    status="action_required",
                    fix="Choose a directory for --workspace; the existing file was preserved.",
                )
            return self._result(ready, evidence={"workspace": str(plan.workspace)})
        if self.name == "user_config":
            path = self._home / "config.yaml"
            if _read(path) is None:
                return self._result(False)
            read_user_config(path)
            return self._result(True, evidence={"path": str(path)})
        if self.name == "runtime_config":
            path = plan.workspace / original.RUNTIME_CONFIG_RELPATH
            content = _read(path)
            if content is None:
                return self._result(False)
            lines = content.splitlines()
            if not lines or lines[0] not in (
                original.SETUP_MARKER,
                original.INSTALL_SH_MARKER,
            ):
                return ModelSetupObservation(
                    step=self.name,
                    status="action_required",
                    fix="The runtime config belongs to the user. Review it before selecting a setup-managed configuration.",
                )
            mode = "docker" if "docker" in plan.components else "local"
            return self._result(
                original.recorded_install_path(path) == mode,
                evidence={"path": str(path), "install_path": mode},
            )
        if self.name == "identity":
            identity = read_local_tenant_identity(
                db_path=LocalByokCredentialStore().db_path
            )
            return self._result(
                identity is not None,
                evidence={"tenant_id": str(identity.tenant_uuid)} if identity else {},
            )
        if not plan.providers:
            return ModelSetupObservation(
                step="models",
                status="action_required",
                fix="Choose provider models with --provider/--model or a secret-free --plan.",
            )
        evidence: dict[str, str] = {}
        receipts: list[ModelOnexSetupReceipt] = []
        for desired in plan.providers:
            if desired.connection_id:
                status = HandlerModelConnections().handle(
                    ModelEndpointConnectionRequest(
                        operation="list", connection_id=desired.connection_id
                    )
                )
                current_connection = (
                    status.connections[0] if status.connections else None
                )
                expected_url = (desired.url or "").rstrip("/").removesuffix("/v1")
                if (
                    current_connection is None
                    or current_connection.url != expected_url
                    or current_connection.models != desired.models
                    or (
                        desired.native_messages is not None
                        and current_connection.native_messages
                        != desired.native_messages
                    )
                ):
                    return self._result(
                        False,
                        fix=f"Configure and prove connection {desired.connection_id}.",
                    )
                supplied_key = self._credentials.get(desired.credential_identity)
                if supplied_key is not None:
                    stored_key = (
                        asyncio.run(
                            LocalByokCredentialStore().get_secret(
                                current_connection.secret_ref
                            )
                        )
                        if current_connection.secret_ref
                        else None
                    )
                    if stored_key != supplied_key.get_secret_value():
                        return self._result(
                            False,
                            fix="Update and prove the changed connection credential.",
                        )
                if desired.make_default and read_first_choice() != (
                    desired.credential_identity,
                    desired.models[0],
                ):
                    return self._result(
                        False,
                        fix="Select the requested named model as the default route.",
                    )
                for model, backend in zip(
                    current_connection.models,
                    current_connection.backend_ids,
                    strict=True,
                ):
                    test = next(
                        (
                            item
                            for item in status.tests
                            if item.backend_id == backend
                            and item.model == model
                            and item.endpoint == expected_url + "/v1/chat/completions"
                            and item.status == "passed"
                        ),
                        None,
                    )
                    if test is None:
                        return self._result(
                            False,
                            fix=f"Prove every model selected for connection {desired.connection_id}.",
                        )
                    evidence[f"{desired.credential_identity}:{model}"] = (
                        f"backend={backend}; tested_at={test.tested_at}"
                    )
                    receipts.append(
                        ModelOnexSetupReceipt(
                            provider=desired.provider,
                            model=model,
                            backend_id=backend,
                            endpoint=test.endpoint,
                            tested_at=test.tested_at,
                        )
                    )
                continue
            statuses = model_keys.list_providers(
                provider=desired.registration_provider,
                url=desired.url,
                models=desired.models,
                read_only=True,
            )
            current = statuses[0] if statuses else None
            test = current.last_test if current else None
            if (
                current is None
                or not current.set_up
                or current.models != desired.models
                or test is None
                or test.status != "passed"
                or test.model != desired.models[0]
            ):
                return self._result(
                    False,
                    fix=f"Configure and prove the selected {desired.provider} models.",
                )
            if desired.url and (test.endpoint or "").rstrip("/").removesuffix(
                "/v1/chat/completions"
            ).removesuffix("/v1") != desired.url.rstrip("/").removesuffix("/v1"):
                return self._result(
                    False,
                    fix=f"Reconcile the changed {desired.provider} endpoint before verification.",
                )
            evidence[desired.provider] = (
                f"{test.model}; backend={test.backend_id}; tested_at={test.tested_at}"
            )
            receipts.append(
                ModelOnexSetupReceipt(
                    provider=desired.provider,
                    model=test.model,
                    backend_id=test.backend_id,
                    endpoint=test.endpoint,
                    tested_at=test.tested_at,
                )
            )
        return self._result(True, evidence=evidence).model_copy(
            update={"receipts": tuple(receipts)}
        )

    def apply(self, plan: ModelSetupPlan) -> ModelSetupObservation | None:
        if self.name == "workspace":
            if plan.workspace.exists() and not plan.workspace.is_dir():
                raise ValueError(
                    "Choose a directory; the existing workspace file was preserved"
                )
            owned = _read(plan.workspace / ".onex-setup-workspace")
            legacy = _read(plan.workspace / ".omninode-dev-setup")
            if owned is not None and owned != "onex setup\n":
                raise ValueError("The workspace marker is foreign and was preserved")
            if (
                plan.workspace.exists()
                and any(plan.workspace.iterdir())
                and owned != "onex setup\n"
                and legacy != "This workspace was created by omninode-dev-setup.\n"
            ):
                raise ValueError("Review the foreign workspace before onboarding")
            original.ensure_workspace(plan.workspace)
            original.write_if_changed(
                plan.workspace / ".onex-setup-workspace", "onex setup\n"
            )
        elif self.name == "user_config":
            original.ensure_user_config(self._home)
        elif self.name == "runtime_config":
            original.ensure_runtime_config(
                plan.workspace,
                "docker" if "docker" in plan.components else "local",
            )
        elif self.name == "identity":
            mint_local_tenant_identity(db_path=LocalByokCredentialStore().db_path)
        else:
            for desired in plan.providers:
                if desired.connection_id:
                    owner = HandlerModelConnections()
                    result = owner.handle(
                        ModelEndpointConnectionRequest(
                            operation="connect",
                            connection_id=desired.connection_id,
                            url=desired.url,
                            models=desired.models,
                            key=self._credentials.get(desired.credential_identity),
                            native_messages=desired.native_messages,
                        )
                    )
                    if result.status != "ready":
                        raise ValueError(
                            result.message
                            or "The named connection did not pass pinned delegation"
                        )
                    if desired.make_default or read_first_choice() is None:
                        selected = owner.handle(
                            ModelEndpointConnectionRequest(
                                operation="select",
                                connection_id=desired.connection_id,
                                models=desired.models[:1],
                            )
                        )
                        if selected.status != "ready":
                            raise ValueError(
                                selected.message
                                or "The connection could not be selected"
                            )
                    continue
                if desired.provider == "ollama" and desired.api_key_required:
                    from omnimarket.handlers.openai_compatible_endpoint import (
                        base_url_of,
                        stored_base_url,
                    )

                    existing = stored_base_url("openai_compatible")
                    if existing and existing != base_url_of(desired.url or ""):
                        raise ValueError(
                            "The authenticated endpoint slot already belongs to another server; "
                            "its URL and credential were preserved. Use a separate ONEX profile."
                        )
                if desired.registration_provider == "local" and len(desired.models) > 1:
                    tests = model_keys.add_local_endpoint(desired.url, desired.models)
                    if not tests or any(test.status != "passed" for test in tests):
                        raise ValueError(
                            "The selected endpoint models did not pass pinned delegation"
                        )
                    continue
                # Preserve the model component's actual pinned delegation and
                # receipt checks. Never substitute a setup marker for inference.
                request = ModelOnexSetupRequest(
                    workspace=plan.workspace,
                    onex_home=self._home,
                    provider=desired.registration_provider,
                    model=desired.models[0],
                    url=desired.url,
                    key=self._credentials.get(desired.provider),
                    unattended=True,
                )
                self._prove_model(request)
                if len(desired.models) > 1:
                    key = self._credentials.get(desired.provider)
                    if key is None:
                        raise ValueError(
                            "Read the provider credential through the setup input first"
                        )
                    model_keys.add_key(
                        desired.registration_provider,
                        key.get_secret_value(),
                        desired.models,
                        url=desired.url,
                    )
        return None
