# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Reconcile native setup using the surviving config, identity and model owners.

The workflow alone writes setup.json. Runtime configuration reuses the original
model-PR implementation with its legacy state writer explicitly disabled.
"""

from __future__ import annotations

import stat
from pathlib import Path
from typing import Literal

from omnibase_core.cli.cli_user_config import read_user_config, user_config_path
from pydantic import SecretStr

from omnimarket.handlers import handler_model_keys as model_keys
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
        info = path.lstat()
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(info.st_mode) or info.st_size > 1024 * 1024:
        raise ValueError("Expected a bounded regular setup file")
    return path.read_text()


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
            adapter.apply(request.plan)
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
            current = statuses.get(desired.provider)
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

    def apply(self, plan: ModelSetupPlan) -> None:
        if self.name == "workspace":
            if (
                plan.workspace.exists()
                and any(plan.workspace.iterdir())
                and _read(plan.workspace / ".onex-setup-workspace") is None
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
                    raise ValueError(
                        "The selected Ollama server must be provisioned before model registration"
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
                if len(desired.models) > 1:
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
