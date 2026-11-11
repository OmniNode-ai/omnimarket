# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
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
import subprocess
import tempfile
from collections.abc import Callable
from pathlib import Path

from omnibase_core.cli.cli_user_config import USER_CONFIG_TEMPLATE, read_user_config
from omnibase_core.errors.model_onex_error import ModelOnexError
from pydantic import ValidationError

from omnimarket.nodes.node_onex_setup_effect.models import (
    ModelOnexSetupFailure,
    ModelOnexSetupReceipt,
    ModelOnexSetupRequest,
    ModelOnexSetupResult,
    ModelOnexSetupState,
    ModelOnexSetupStepOutcome,
)

SETUP_STEPS: tuple[str, ...] = (
    "workspace",
    "prerequisites",
    "user_config",
    "runtime_config",
    "model",
)
#: The providers setup offers. Ollama is not one: a user who runs it names it as
#: ``--provider local --url <its OpenAI-compatible base URL>``.
SETUP_PROVIDERS: tuple[str, ...] = (
    "gemini",
    "openrouter",
    "openai",
    "openai_compatible",
    "local",
    "ollama",
)
#: Providers setup refuses, each with its problem and the way to reach it instead.
REFUSED_PROVIDERS: dict[str, tuple[str, str]] = {}
URL_PROVIDERS: tuple[str, ...] = ("local", "openai_compatible", "ollama")
KEYLESS_PROVIDERS: tuple[str, ...] = ("local", "ollama")
ARGUMENTS_STEP = "arguments"
SETUP_MARKER = "# generated-by: onex setup"
INSTALL_SH_MARKER = "# generated-by: omnibase install.sh"
INSTALL_PATHS: tuple[str, ...] = ("local", "docker")
RUNTIME_CONFIG_RELPATH = Path("config/onex/runtime/runtime_config.yaml")
STATE_FILE_NAME = "setup.json"
USER_CONFIG_FILE_NAME = "config.yaml"
_OWNED_MARKERS = (SETUP_MARKER, INSTALL_SH_MARKER)

_RUNTIME_CONFIG_TEMPLATE = """\
{marker}
# install-path: {path}
#
# This workspace's runtime configuration, written for the install path above.
# Rewrite it for another path with:  onex setup --path <local|docker>
# Hand edits are kept only if you delete the first line, after which onex setup
# never touches it.
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

_DOCKER_TOOLS = {
    "docker": "install Docker (https://docs.docker.com/get-docker/), or use --path local, "
    "which needs no Docker",
    "openssl": "install openssl (macOS: brew install openssl@3)",
    "make": "install make (macOS: xcode-select --install)",
}


def which(name: str) -> str | None:
    """Where ``name`` is on PATH, or None. A seam the tests replace."""
    return shutil.which(name)


def compose_available() -> bool:
    """True when ``docker compose version`` succeeds. A seam the tests replace."""
    try:
        completed = subprocess.run(
            ["docker", "compose", "version"],
            capture_output=True,
            check=False,
        )
    except OSError:
        return False
    return completed.returncode == 0


class _StepFailedError(Exception):
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
    lines = runtime_config.read_text(encoding="utf-8").splitlines()
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
        raise _StepFailedError(
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
        raise _StepFailedError(
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
        raise _StepFailedError(
            "prerequisites",
            "the cloud install path is not available yet",
            "use --path local (no Docker) or --path docker",
        )
    if choice not in INSTALL_PATHS:
        raise _StepFailedError(
            "prerequisites",
            f"unknown install path '{choice}'",
            "use --path local (no Docker) or --path docker",
        )
    return choice


def _step_prerequisites(install_path: str) -> str:
    if install_path == "local":
        return "path local needs nothing beyond this CLI"
    for tool, remedy in _DOCKER_TOOLS.items():
        if which(tool) is None:
            raise _StepFailedError(
                "prerequisites",
                f"the docker install path needs {tool}, which is not on PATH",
                remedy,
            )
        if tool == "docker" and not compose_available():
            raise _StepFailedError(
                "prerequisites",
                "the docker install path needs the docker compose plugin",
                "install the Docker Compose plugin (docker compose version must succeed), "
                "or use --path local",
            )
    return "path docker: docker, docker compose, openssl and make found"


def ensure_user_config(onex_home: Path) -> tuple[str, bool]:
    config = onex_home / USER_CONFIG_FILE_NAME
    if config.exists():
        try:
            read_user_config(config)
        except (ModelOnexError, OSError) as exc:
            raise _StepFailedError(
                "user_config",
                f"{config} exists but cannot be read as a config ({exc})",
                f"repair {config}, or move it aside so setup writes a fresh one",
            ) from exc
        return f"{config} kept as is", False
    for sub in ("state", "logs"):
        (onex_home / sub).mkdir(parents=True, exist_ok=True)
    _write_atomic(config, USER_CONFIG_TEMPLATE)
    return str(config), True


def ensure_runtime_config(workspace: Path, install_path: str) -> tuple[Path, bool]:
    runtime_config = workspace / RUNTIME_CONFIG_RELPATH
    if runtime_config.exists():
        lines = runtime_config.read_text(encoding="utf-8").splitlines()
        if not lines or lines[0] not in _OWNED_MARKERS:
            raise _StepFailedError(
                "runtime_config",
                f"{runtime_config} exists and was not written by onex setup or "
                "install.sh; it is left untouched",
                f"move {runtime_config} aside so setup can write the {install_path} "
                "configuration",
            )
    bus_type, description = _PATH_SHAPES[install_path]
    content = _RUNTIME_CONFIG_TEMPLATE.format(
        marker=SETUP_MARKER,
        path=install_path,
        description=description,
        bus_type=bus_type,
    )
    written = write_if_changed(runtime_config, content)
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
        raise _StepFailedError(
            "model",
            str(refusal),
            "check the key and the model against the provider's own pages"
            if provider not in KEYLESS_PROVIDERS
            else "check that the server is up at --url and runs that model",
        ) from None
    if result is None or result.status != "passed":
        reason = (result.reason if result is not None else None) or "no delegation ran"
        throttled = result is not None and result.status == "throttled"
        raise _StepFailedError(
            "model",
            f"the test delegation did not answer on {provider}/{model}: {reason}",
            "the provider is throttling; run the retry command again shortly"
            if throttled
            else "fix the key or the server, then run the retry command again",
        )
    if result.model != model:
        raise _StepFailedError(
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
        if not request.provider or not request.model:
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
                            "api_key_required": request.provider.lower() == "ollama"
                            and request.key is not None,
                        }
                    ],
                }
            )
            credentials = (
                {request.provider.lower(): request.key}
                if request.key is not None
                else {}
            )
            journal = HandlerSetupWorkflow(
                onex_home=request.onex_home,
                credentials=credentials,
                model_step=self._model_step,
            ).handle(plan)
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
    "compose_available",
    "prove_model",
    "recorded_state",
    "refused_before_any_step",
    "retry_command",
    "which",
]
