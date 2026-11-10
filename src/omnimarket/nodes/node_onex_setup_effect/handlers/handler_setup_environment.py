# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Reconcile native setup using the surviving config, identity and model owners.

The workflow alone writes setup.json. Runtime configuration reuses the original
model-PR implementation with its legacy state writer explicitly disabled.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path
from typing import Literal

import yaml
from omnibase_core.cli.cli_user_config import read_user_config, user_config_path
from pydantic import SecretStr

from omnimarket.handlers import handler_model_keys as model_keys
from omnimarket.inference.local_byok_credential_adapter import LocalByokCredentialStore
from omnimarket.local_deployment.tenant_identity import (
    mint_local_tenant_identity,
    read_local_tenant_identity,
)
from omnimarket.nodes.node_model_overlay_write_effect.handlers.handler_model_overlay_write import (
    HandlerModelOverlayWrite,
)
from omnimarket.nodes.node_model_overlay_write_effect.models.model_model_overlay_write_request import (
    ModelModelOverlayWriteRequest,
)
from omnimarket.nodes.node_model_overlay_write_effect.protocols.protocol_overlay_file_store_backends import (
    FilesystemOverlayFileStore,
    InMemoryOverlayFileStore,
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


def reconcile_ollama_overlay(
    plan: ModelSetupPlan, *, write: bool = False, tested_endpoint: str | None = None
) -> bool:
    """Compose the existing overlay owner without erasing other local routes.

    The explicit model selects its canonical tier. Native resource suitability is
    checked by the preceding Ollama adapter, before this configuration effect.
    """
    selected = next(
        (item for item in plan.providers if item.provider == "ollama"), None
    )
    if selected is None:
        return True
    path = model_keys.model_handler().overrides_path()
    request = ModelModelOverlayWriteRequest(
        mode="native",
        source="ollama",
        memory_gb=0,
        served_model_id=selected.models[0],
        target_path=path,
        marker_line="# Written by omninode-dev-setup: Ollama on this Mac.",
    )
    preview = InMemoryOverlayFileStore()
    result = HandlerModelOverlayWrite(preview).handle(request)
    if result.outcome == "refused":
        raise ValueError(
            "The canonical model configuration refused the selected Ollama model"
        )
    expected = yaml.safe_load(preview.files[path])
    raw = _read(path)
    try:
        actual = yaml.safe_load(raw) if raw is not None else {}
    except yaml.YAMLError:
        raise ValueError(
            "Review the existing model overlay; its unreadable content was preserved"
        ) from None
    if not isinstance(actual, dict) or not isinstance(actual.get("backends", []), list):
        raise ValueError(
            "Review the existing model overlay; its foreign structure was preserved"
        )
    rows = actual.get("backends", [])
    wanted = {row["backend_id"]: row for row in expected["backends"]}
    if tested_endpoint is not None and tested_endpoint not in {
        row["endpoint_url"] for row in wanted.values()
    }:
        return False
    current = {row.get("backend_id"): row for row in rows if isinstance(row, dict)}
    ready = all(
        name in current
        and all(current[name].get(key) == value for key, value in row.items())
        for name, row in wanted.items()
    )
    if ready or not write:
        return ready
    if any(
        not isinstance(row, dict) or row.get("backend_id") not in wanted for row in rows
    ) or len(current) != len(rows):
        raise ValueError(
            "Review the existing local routes before changing Ollama; unrelated routes were preserved"
        )
    result = HandlerModelOverlayWrite(FilesystemOverlayFileStore()).handle(request)
    if result.outcome == "refused":
        raise ValueError(
            "The existing model overlay is user-owned and was preserved; review it before changing Ollama"
        )
    return reconcile_ollama_overlay(plan)


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
        statuses = {item.provider: item for item in model_keys.list_providers()}
        evidence: dict[str, str] = {}
        receipts: list[ModelOnexSetupReceipt] = []
        for desired in plan.providers:
            if desired.provider == "ollama":
                try:
                    ready = reconcile_ollama_overlay(plan)
                except ValueError as exc:
                    return ModelSetupObservation(
                        step="models", status="action_required", fix=str(exc)
                    )
                if not ready:
                    return self._result(
                        False,
                        fix="Reconcile the native Ollama routing overlay before model verification.",
                    )
            current = statuses.get(desired.provider)
            test = current.last_test if current else None
            if (
                current is None
                or not current.set_up
                or (desired.provider != "ollama" and current.models != desired.models)
                or test is None
                or test.status != "passed"
                or test.model != desired.models[0]
            ):
                return self._result(
                    False,
                    fix=f"Configure and prove the selected {desired.provider} models.",
                )
            if desired.provider == "ollama" and (
                test.endpoint is None
                or not reconcile_ollama_overlay(plan, tested_endpoint=test.endpoint)
            ):
                return self._result(
                    False,
                    fix="Prove the selected Ollama model on this Mac; its previous test used another endpoint.",
                )
            if (
                desired.provider != "ollama"
                and desired.url
                and (test.endpoint or "")
                .rstrip("/")
                .removesuffix("/v1/chat/completions")
                .removesuffix("/v1")
                != desired.url.rstrip("/").removesuffix("/v1")
            ):
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
                if desired.provider == "ollama":
                    try:
                        if not reconcile_ollama_overlay(plan, write=True):
                            raise ValueError(
                                "Ollama routing did not pass actual configuration readback"
                            )
                    except ValueError as exc:
                        return ModelSetupObservation(
                            step="models", status="action_required", fix=str(exc)
                        )
                if desired.provider == "local" and len(desired.models) != 1:
                    raise ValueError(
                        "The existing local registration API accepts one model per server"
                    )
                # Preserve the model component's actual pinned delegation and
                # receipt checks. Never substitute a setup marker for inference.
                request = ModelOnexSetupRequest(
                    workspace=plan.workspace,
                    onex_home=self._home,
                    provider=desired.provider,
                    model=desired.models[0],
                    url=desired.url,
                    key=self._credentials.get(desired.provider),
                    unattended=True,
                )
                self._prove_model(request)
                if len(desired.models) > 1 and desired.provider != "ollama":
                    key = self._credentials.get(desired.provider)
                    if key is None:
                        raise ValueError(
                            "Read the provider credential through the setup input first"
                        )
                    model_keys.add_key(
                        desired.provider,
                        key.get_secret_value(),
                        desired.models,
                        url=desired.url,
                    )
        return None
