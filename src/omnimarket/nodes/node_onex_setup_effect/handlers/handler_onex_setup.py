# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
# unimported-handler-allow: HandlerOnexSetup is resolved at boot from handler_routing in contract.yaml, by module path, not by a Python import
"""HandlerOnexSetup: run the onboarding walk a developer needs after installing the CLI.

Steps, in order (``SETUP_STEPS``):

``workspace``       create the workspace root (OMNIBASE_PATH)
``prerequisites``   choose the install path and check what that path needs
``user_config``     write ``<onex_home>/config.yaml`` when it does not exist
``runtime_config``  write the workspace runtime configuration, then record the
                    workspace and path in ``<onex_home>/setup.json``
``model``           store the user's key through the handler ``onex models add``
                    uses, run one delegation pinned to it and read its receipt
                    back; the walk ends only when that delegation answered on the
                    provider and model named

Every step but ``model`` is idempotent and every write is atomic (a temporary file renamed
into place), so a walk that stops at a failed step leaves nothing half-written
and the next run resumes from that step. A second run on a configured machine
writes nothing; ``model`` stores the key again and proves the delegation again, which
is what a retry with a corrected key needs. Which omnibase install.sh step went where is recorded in
``setup_step_inventory.yaml`` beside this node's contract.
"""

from __future__ import annotations

import os
import shlex
import shutil
import tempfile
import threading
import uuid
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from omnibase_core.cli.cli_user_config import USER_CONFIG_TEMPLATE, read_user_config
from omnibase_core.errors.model_onex_error import ModelOnexError
from omnibase_infra.handlers.handler_workspace_config_root_resolve import (
    DEVELOPER_WORKSPACE_CONFIG_RELATIVE_PATH,
)
from pydantic import SecretStr, ValidationError

from omnimarket.handlers.routing.byok_provider_backends import (
    customer_provider_catalogue,
)
from omnimarket.nodes.node_onex_setup_effect.models import (
    ModelOnexSetupFailure,
    ModelOnexSetupReceipt,
    ModelOnexSetupRequest,
    ModelOnexSetupResult,
    ModelOnexSetupState,
    ModelOnexSetupStepOutcome,
)
from omnimarket.nodes.node_onex_setup_effect.models.model_setup_provider_traits import (
    setup_provider_traits,
)

SETUP_STEPS: tuple[str, ...] = (
    "workspace",
    "prerequisites",
    "user_config",
    "runtime_config",
    "model",
)
#: Hosted choices come from the canonical provider registry. A Local Model is an
#: endpoint-only setup mode (any model server, by URL); it provisions no software.
SETUP_PROVIDERS: tuple[str, ...] = tuple(
    dict.fromkeys((*customer_provider_catalogue(), "local"))
)
#: Providers setup refuses, each with its problem and the way to reach it instead.
REFUSED_PROVIDERS: dict[str, tuple[str, str]] = {}
URL_PROVIDERS: tuple[str, ...] = ("local", "openai_compatible")
KEYLESS_PROVIDERS: tuple[str, ...] = ("local",)
ARGUMENTS_STEP = "arguments"
SETUP_MARKER = "# generated-by: onex setup"
INSTALL_SH_MARKER = "# generated-by: omnibase install.sh"
INSTALL_PATHS: tuple[str, ...] = ("local", "docker")
#: Where onex delegate reads a developer workspace's runtime config: the config
#: root omnibase_infra resolves for it (.onex/workspace-config, OMN-20700). Setup
#: wrote <ws>/config/onex/runtime/ before, which delegate never read, so
#: `OMNIBASE_PATH=<ws> onex delegate` refused the workspace (OMN-23413). A
#: checkout's materialised config still outranks this one in delegate.
RUNTIME_CONFIG_RELPATH = DEVELOPER_WORKSPACE_CONFIG_RELATIVE_PATH / Path(
    "config/onex/runtime/runtime_config.yaml"
)
STATE_FILE_NAME = "setup.json"
USER_CONFIG_FILE_NAME = "config.yaml"
_OWNED_MARKERS = (SETUP_MARKER, INSTALL_SH_MARKER)

_PREVIOUS_EDIT_NOTE = (
    "# Hand edits are kept only if you delete the first line, after which onex setup\n"
    "# never touches it."
)
_EDIT_NOTE = "# Setup keeps hand edits. Review or move an edited file before changing install paths."

_RUNTIME_CONFIG_TEMPLATE = """\
{marker}
# install-path: {path}
#
# This workspace's runtime configuration, written for the install path above.
# Rewrite it for another path with:  onex setup --path <local|docker>
{edit_note}
name: "omnibase-{path}"
description: "{description}"
input_topic: "requests"
output_topic: "responses"
group_id: "onex-runtime"
event_bus:
  type: "{bus_type}"
  profile: "local"
  environment: "local"
  max_history: 1000
  circuit_breaker_threshold: 5
"""

_PATH_SHAPES = {
    "local": (
        "inmemory",
        "onex setup path local: fully local runtime, in-memory event bus, no Docker",
    ),
    "docker": (
        "kafka",
        "onex setup path docker: self-hosted Docker stack, Kafka-compatible event bus",
    ),
}


def which(name: str) -> str | None:
    """Where ``name`` is on PATH, or None. A seam the tests replace."""
    return shutil.which(name)


class StepFailedError(Exception):
    """One step could not complete; carries its single fix."""

    def __init__(self, step: str, problem: str, fix: str) -> None:
        super().__init__(problem)
        self.step = step
        self.problem = problem
        self.fix = fix


def _write_atomic(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
        tmp.replace(path)
    finally:
        tmp.unlink(missing_ok=True)


def write_if_changed(path: Path, content: str) -> bool:
    """Write ``content`` unless the file already holds it. True when written."""
    if path.is_file() and path.read_text(encoding="utf-8") == content:
        return False
    _write_atomic(path, content)
    return True


def recorded_state(onex_home: Path) -> ModelOnexSetupState | None:
    """The workspace and path an earlier completed walk recorded, or None."""
    path = onex_home / STATE_FILE_NAME
    if not path.is_file():
        return None
    try:
        return ModelOnexSetupState.model_validate_json(path.read_text(encoding="utf-8"))
    except ValidationError:
        return None


def recorded_install_path(runtime_config: Path) -> str | None:
    if not runtime_config.is_file():
        return None
    return runtime_config_install_path(runtime_config.read_text(encoding="utf-8"))


def runtime_config_install_path(content: str) -> str | None:
    lines = content.splitlines()
    if not lines or lines[0] not in _OWNED_MARKERS:
        return None
    for line in lines[1:4]:
        if line.startswith("# install-path:"):
            value = line.split(":", 1)[1].strip()
            return value if value in INSTALL_PATHS else None
    return None


def retry_command(
    workspace: Path | None,
    install_path: str | None,
    request: ModelOnexSetupRequest | None = None,
) -> str:
    """The full command that resumes the walk. It never carries a key: that is stdin's."""
    args = ["onex", "setup"]
    if request is not None and request.unattended:
        args.append("--no-interactive")
    args += ["--workspace", str(workspace) if workspace else "DIR"]
    if install_path in INSTALL_PATHS:
        args += ["--path", str(install_path)]
    if request is not None:
        if request.provider:
            args += ["--provider", request.provider]
        if request.model:
            args += ["--model", request.model]
        if request.url:
            args += ["--url", request.url]
    return shlex.join(args)


def _refuse(
    request: ModelOnexSetupRequest,
    problem: str,
    fix: str,
    *,
    retry_request: ModelOnexSetupRequest | None = None,
) -> ModelOnexSetupResult:
    retry_with = retry_request or request
    return ModelOnexSetupResult(
        workspace=request.workspace,
        install_path=request.install_path,
        steps=(),
        failure=ModelOnexSetupFailure(
            step=ARGUMENTS_STEP,
            problem=problem,
            fix=fix,
            retry=retry_command(request.workspace, request.install_path, retry_with),
        ),
    )


def refused_before_any_step(
    request: ModelOnexSetupRequest,
) -> ModelOnexSetupResult | None:
    """The refusal for a request that cannot complete, raised before any step runs.

    A provider setup does not offer is refused in every mode. In the unattended
    form every choice must be named, so the first missing one is refused here,
    naming its flag, with nothing written.
    """
    provider = request.provider.lower() if request.provider else None
    if provider in REFUSED_PROVIDERS:
        problem, fix = REFUSED_PROVIDERS[provider]
        return _refuse(
            request,
            problem,
            fix,
            retry_request=request.model_copy(update={"provider": None}),
        )
    if provider is not None and provider not in SETUP_PROVIDERS:
        return _refuse(
            request,
            f"unknown provider '{request.provider}'",
            f"use --provider with one of: {', '.join(SETUP_PROVIDERS)}",
        )
    if not request.unattended:
        return None
    if request.workspace is None:
        return _refuse(
            request,
            "--workspace is missing: no workspace root is named, and there is no "
            "terminal to ask in",
            "pass --workspace DIR, or export OMNIBASE_PATH=DIR",
        )
    if provider is None and not request.model and not request.url:
        return None  # no model server named: the models step is skipped, not refused
    if provider is None:
        return _refuse(
            request,
            "--provider is missing",
            f"pass --provider with one of: {', '.join(SETUP_PROVIDERS)}",
        )
    if not request.model:
        return _refuse(
            request,
            "--model is missing: nothing is chosen for you",
            f"pass --model with a model your {provider} "
            + ("server runs" if provider in KEYLESS_PROVIDERS else "key can run"),
        )
    if provider in URL_PROVIDERS and not request.url:
        return _refuse(
            request,
            f"--url is missing: --provider {provider} needs the server's base URL",
            f"pass --url http://<host>:<port> with --provider {provider}",
        )
    if provider not in KEYLESS_PROVIDERS and (
        request.key is None or not request.key.get_secret_value().strip()
    ):
        return _refuse(
            request,
            f"no {provider} key was given on stdin",
            "pipe the key in on stdin, for example from your secret manager's "
            "read command; it is never accepted as an argument",
        )
    return None


def ensure_workspace(workspace: Path | None) -> tuple[Path, bool]:
    if workspace is None:
        raise StepFailedError(
            "workspace",
            "no workspace root is named, and there is no terminal to ask in",
            "pass --workspace DIR, or export OMNIBASE_PATH=DIR, naming the directory "
            "your ONEX workspace lives in",
        )
    root = workspace.expanduser().resolve()
    existed = root.is_dir()
    try:
        root.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise StepFailedError(
            "workspace",
            f"cannot use {root} as the workspace root ({exc.strerror or exc})",
            f"move aside whatever is at {root}, or pass --workspace with another directory",
        ) from exc
    return root, not existed


def _resolve_path(
    given: str | None, state: ModelOnexSetupState | None, runtime_config: Path
) -> str:
    if given is not None:
        choice = given
    elif state is not None:
        choice = state.install_path
    else:
        choice = recorded_install_path(runtime_config) or "local"
    if choice == "cloud":
        raise StepFailedError(
            "prerequisites",
            "the cloud install path is not available yet",
            "use --path local (no Docker) or --path docker",
        )
    if choice not in INSTALL_PATHS:
        raise StepFailedError(
            "prerequisites",
            f"unknown install path '{choice}'",
            "use --path local (no Docker) or --path docker",
        )
    return choice


def ensure_user_config(onex_home: Path) -> tuple[str, bool]:
    config = onex_home / USER_CONFIG_FILE_NAME
    if config.exists():
        try:
            read_user_config(config)
        except (ModelOnexError, OSError) as exc:
            raise StepFailedError(
                "user_config",
                f"{config} exists but cannot be read as a config ({exc})",
                f"repair {config}, or move it aside so setup writes a fresh one",
            ) from exc
        return f"{config} kept as is", False
    for sub in ("state", "logs"):
        (onex_home / sub).mkdir(parents=True, exist_ok=True)
    _write_atomic(config, USER_CONFIG_TEMPLATE)
    return str(config), True


def _generated_runtime_config(install_path: str, edit_note: str = _EDIT_NOTE) -> str:
    bus_type, description = _PATH_SHAPES[install_path]
    return _RUNTIME_CONFIG_TEMPLATE.format(
        marker=SETUP_MARKER,
        edit_note=edit_note,
        path=install_path,
        description=description,
        bus_type=bus_type,
    )


def runtime_config_can_switch(content: str) -> bool:
    """Only a known generator's complete output can be replaced automatically."""
    return any(
        content == _generated_runtime_config(mode, note)
        for mode in INSTALL_PATHS
        for note in (_EDIT_NOTE, _PREVIOUS_EDIT_NOTE)
    )


def ensure_runtime_config(workspace: Path, install_path: str) -> tuple[Path, bool]:
    runtime_config = workspace / RUNTIME_CONFIG_RELPATH
    if runtime_config.exists():
        existing = runtime_config.read_bytes().decode()
        if runtime_config_install_path(existing) == install_path:
            return runtime_config, False
        if not runtime_config_can_switch(existing):
            raise StepFailedError(
                "runtime_config",
                f"{runtime_config} contains an existing configuration that setup cannot replace.",
                f"Review {runtime_config} or move it aside before changing the install path.",
            )
    written = write_if_changed(runtime_config, _generated_runtime_config(install_path))
    return runtime_config, written


def prove_model(request: ModelOnexSetupRequest) -> ModelOnexSetupReceipt:
    """Store the key through the handler ``onex models add`` uses, then read one delegation's receipt.

    The delegation is the pinned test ``onex models add`` runs (one ``onex delegate``
    pinned to the provider's backend). The step passes only when it answered, on the
    provider and the model named; nothing is passed to it but the backend id.
    """
    from omnimarket.handlers import handler_model_keys as model_keys
    from omnimarket.handlers.handler_local_model_setup import (
        HandlerLocalModelSetup,
        ModelLocalModelSetupRequest,
    )
    from omnimarket.nodes.node_model_setup_effect.handlers.handler_model_setup import (
        ModelSetupRefusedError,
    )

    provider = (request.provider or "").lower()
    model = request.model or ""
    try:
        if provider in KEYLESS_PROVIDERS:
            result = HandlerLocalModelSetup().handle(
                ModelLocalModelSetupRequest(url=request.url, model=model)
            )
        else:
            key = request.key.get_secret_value() if request.key is not None else ""
            result = model_keys.add_key(provider, key, (model,), url=request.url)
    except (model_keys.ModelKeyRefusedError, ModelSetupRefusedError) as refusal:
        raise StepFailedError(
            "model",
            str(refusal),
            "check the key and the model against the provider's own pages"
            if provider not in KEYLESS_PROVIDERS
            else "check that the server is up at --url and runs that model",
        ) from None
    if result is None or result.status != "passed":
        reason = (result.reason if result is not None else None) or "no delegation ran"
        throttled = result is not None and result.status == "throttled"
        raise StepFailedError(
            "model",
            f"the test delegation did not answer on {provider}/{model}: {reason}",
            "the provider is throttling; run the retry command again shortly"
            if throttled
            else "fix the key or the server, then run the retry command again",
        )
    if result.model != model:
        raise StepFailedError(
            "model",
            f"the delegation answered on model '{result.model}', not the '{model}' named",
            f"check which models {provider} offers your key, then name one with --model",
        )
    return ModelOnexSetupReceipt(
        provider=provider,
        model=result.model,
        backend_id=result.backend_id,
        endpoint=result.endpoint,
        tested_at=result.tested_at,
    )


ModelStep = Callable[[ModelOnexSetupRequest], ModelOnexSetupReceipt]


def _off_event_loop[T](call: Callable[[], T]) -> T:
    """Run ``call`` on a worker thread and return its result (a raised error propagates).

    The runtime calls this handler on its running event loop, and the model step stores the key
    through code that starts its own loop with asyncio.run, which refuses to start on a thread
    that already runs one.
    """
    with ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(call).result()


# The CLI hosts this node on the local runtime, which records every command it carries, so the
# provider key cannot ride on the command. The CLI parks it here, in this process only, and the
# command carries the token; handle() takes the key back out before any step runs.
_PARKED_KEYS: dict[str, SecretStr] = {}
_PARKED_KEYS_LOCK = threading.Lock()


def park_key(key: SecretStr) -> str:
    """Hold ``key`` in this process and return the token that names it."""
    token = uuid.uuid4().hex
    with _PARKED_KEYS_LOCK:
        _PARKED_KEYS[token] = key
    return token


def take_key(token: str | None) -> SecretStr | None:
    """Remove and return the key parked under ``token``; None for no token or an unknown one."""
    if token is None:
        return None
    with _PARKED_KEYS_LOCK:
        return _PARKED_KEYS.pop(token, None)


def discard_key(token: str | None) -> None:
    """Drop a parked key nobody took."""
    take_key(token)


class HandlerOnexSetup:
    """Keep the v1 contract shape while dispatching to the one setup engine."""

    def __init__(self, model_step: ModelStep | None = None) -> None:
        self._model_step: ModelStep = model_step or prove_model

    def handle(self, request: ModelOnexSetupRequest) -> ModelOnexSetupResult:
        from omnimarket.nodes.node_onex_setup_effect.handlers.handler_setup_workflow import (
            HandlerSetupWorkflow,
        )
        from omnimarket.nodes.node_onex_setup_effect.models.model_setup_plan import (
            ModelSetupPlan,
        )

        # OMN-24432 (dev): the CLI parks the key and the command carries only its
        # token, because the local runtime records every command.
        if request.key is None and request.key_handoff is not None:
            request = request.model_copy(
                update={"key": take_key(request.key_handoff), "key_handoff": None}
            )
        refused = refused_before_any_step(request)
        if refused is not None:
            return refused
        if request.workspace is None:
            return ModelOnexSetupResult(
                workspace=None,
                install_path=request.install_path,
                steps=(),
                failure=ModelOnexSetupFailure(
                    step="workspace",
                    problem="No workspace root was named.",
                    fix="Pass --workspace DIR.",
                    retry=retry_command(None, request.install_path, request),
                ),
            )
        named = bool(request.provider or request.model or request.url)
        if named and (not request.provider or not request.model):
            return _refuse(
                request,
                "A provider and model are required.",
                "Name --provider and --model before starting setup.",
            )
        if request.install_path not in (None, "local"):
            return _refuse(
                request,
                "The requested install lifecycle is not integrated yet.",
                "Use --path local; Docker lifecycle setup is unfinished.",
            )
        try:
            plan = ModelSetupPlan.model_validate(
                {
                    "workspace": request.workspace,
                    "providers": [
                        {
                            "provider": request.provider.lower(),
                            "models": [request.model],
                            "url": request.url,
                            "api_key_required": request.provider.lower()
                            in setup_provider_traits().endpoint_auth
                            and request.key is not None,
                        }
                    ]
                    if request.provider and request.model
                    else [],
                }
            )
            credentials = (
                {request.provider.lower(): request.key}
                if request.provider and request.key is not None
                else {}
            )
            # The runtime calls handle() on its event loop and the model step
            # starts its own (asyncio.run), so the workflow runs on a worker thread.
            journal = _off_event_loop(
                lambda: HandlerSetupWorkflow(
                    onex_home=request.onex_home,
                    credentials=credentials,
                    model_step=self._model_step,
                ).handle(plan)
            )
        except (OSError, ValueError):
            return _refuse(
                request,
                "The setup request or state could not be validated.",
                "Preserve existing setup files and review the selected workspace, provider, model and URL.",
            )
        outcomes = tuple(
            ModelOnexSetupStepOutcome(
                step=item.step,
                changed=item.changed,
                detail="; ".join(
                    f"{key}: {value}" for key, value in item.evidence.items()
                )
                or item.status,
            )
            for item in journal.observations
            if item.status in ("success", "skipped")
        )
        failed = next(
            (
                item
                for item in journal.observations
                if item.status not in ("success", "skipped")
            ),
            None,
        )
        receipts = [
            receipt for item in journal.observations for receipt in item.receipts
        ]
        return ModelOnexSetupResult(
            workspace=journal.workspace,
            install_path=journal.install_path,
            steps=outcomes,
            failure=ModelOnexSetupFailure(
                step=failed.step,
                problem=f"Component reported {failed.status}.",
                fix=failed.fix or "Review the failed component.",
                retry=failed.retry
                or retry_command(journal.workspace, journal.install_path, request),
            )
            if failed
            else None,
            receipt=receipts[0] if receipts and failed is None else None,
        )


__all__ = [
    "INSTALL_PATHS",
    "INSTALL_SH_MARKER",
    "KEYLESS_PROVIDERS",
    "REFUSED_PROVIDERS",
    "RUNTIME_CONFIG_RELPATH",
    "SETUP_MARKER",
    "SETUP_PROVIDERS",
    "SETUP_STEPS",
    "URL_PROVIDERS",
    "HandlerOnexSetup",
    "StepFailedError",
    "discard_key",
    "park_key",
    "prove_model",
    "recorded_state",
    "refused_before_any_step",
    "retry_command",
    "take_key",
    "which",
]
