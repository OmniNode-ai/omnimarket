# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Report and test the models delegation can use (OMN-20817).

A developer can use any combination of Gemini, OpenRouter, OpenAI, Anthropic
and Ollama;
delegation chooses among the ones set up. This effect answers two questions for
``onex models`` and for onboarding, which calls it once per chosen model:

* which providers are set up on this machine: a key stored for a keyed provider
  (the same registration ``onex secret set`` makes), or, for Ollama, local
  routes pointing at Ollama's port (the ``ollama`` block of the model config);
* does one answer: a single delegation pinned to its backend with ``onex
  delegate --backend-id``. The pin is pin-or-refuse, so a run answered by any
  other backend fails, and a pass proves this provider answered.

Each test is recorded beside the local store (``model_tests.json``) so ``onex
models list`` can show the last result. Nothing here reads or carries a key: the
delegation resolves it at the effect boundary, as every delegation does.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import get_args

import yaml

from omnimarket.handlers.node_overlay_reader import (
    NodeOverlayError,
    load_node_overlay,
    resolve_setting,
)
from omnimarket.inference.local_byok_credential_adapter import (
    registered_local_byok_providers,
    resolve_local_byok_credential_model,
    resolve_local_byok_credential_models,
)
from omnimarket.models.delegation.llm_cost_routing import ModelLlmModelRegistryLoader
from omnimarket.nodes.node_model_setup_effect.models.model_model_setup_request import (
    ModelModelSetupRequest,
    ModelProvider,
)
from omnimarket.nodes.node_model_setup_effect.models.model_model_setup_result import (
    ModelModelSetupResult,
    ModelModelStatus,
    ModelModelTestResult,
)
from omnimarket.projection.sqlite_database import default_evidence_db_path
from omnimarket.routing.byok_provider_backends import resolve_byok_provider_backend

PROVIDERS: tuple[ModelProvider, ...] = get_args(ModelProvider)

#: The backend onboarding points at Ollama.
OLLAMA_BACKEND = "local-coder"

#: Leading characters of each provider's keys, most specific first: an
#: OpenRouter or Anthropic key also starts with ``sk-``.
_KEY_PREFIXES: tuple[tuple[str, str], ...] = (
    ("sk-or-", "openrouter"),
    ("sk-ant-", "anthropic"),
    ("AIza", "gemini"),
    ("sk-", "openai"),
)

#: The node whose overlay (``<root>/node_model_setup_effect/overlay.yaml``) declares
#: the model onboarding registers (OMN-20844). No model id is written in code: the
#: value is read from the environment variable, else the overlay, else the default
#: this node's contract ships, so a first run needs no private overlay.
DEFAULT_MODEL_NODE = "node_model_setup_effect"
_DEFAULT_MODEL_ENV: dict[str, str] = {"openrouter": "ONEX_OPENROUTER_DEFAULT_MODEL"}
_DEFAULT_MODEL_FIELDS = frozenset(f"{p}_default_model" for p in _DEFAULT_MODEL_ENV)

_TEST_PROMPT = "Reply with exactly one word: hello"
_TEST_TIMEOUT_S = 300
_MODEL_CONFIG = Path(__file__).resolve().parents[3] / "configs/bifrost_delegation.yaml"
_CONTRACT = Path(__file__).resolve().parents[1] / "contract.yaml"


def packaged_default(setting: str) -> str:
    """The model the registry entry named by ``config.<setting>.registry_model`` serves.

    The contract names a registry entry rather than a model: model ids live only
    in the model registry. ``""`` when either is missing.
    """
    try:
        contract = yaml.safe_load(_CONTRACT.read_text(encoding="utf-8")) or {}
    except (OSError, UnicodeError, yaml.YAMLError):
        return ""
    entry = (contract.get("config") or {}).get(setting) or {}
    key = entry.get("registry_model") if isinstance(entry, dict) else None
    if not isinstance(key, str) or not key.strip():
        return ""
    try:
        profile = ModelLlmModelRegistryLoader().load().get_model(key.strip())
    except (KeyError, OSError, ValueError):
        return ""
    return (profile.model_name or "").strip()


#: (backend id) -> (exit code, stdout, stderr) of one pinned ``onex delegate``.
DelegateRunner = Callable[[str], tuple[int, str, str]]

#: (provider, the whole ordered list) -> the list as stored. The write belongs to
#: node_local_secret_store_effect; the shim that runs both nodes passes it in, so
#: this node never imports that node's handler. It raises
#: ``ModelSetupRefusedError`` for a write the store refuses.
ModelListWriter = Callable[[str, tuple[str, ...]], tuple[str, ...]]


class ModelSetupRefusedError(Exception):
    """A model-list edit the setup effect refuses. Its text says what to run instead."""


def key_looks_like(value: str) -> str | None:
    """The provider whose keys start like ``value``, or ``None`` when unknown."""
    for prefix, provider in _KEY_PREFIXES:
        if value.startswith(prefix):
            return provider
    return None


def _onex_executable() -> str:
    beside = Path(sys.executable).with_name("onex")
    if beside.is_file():
        return str(beside)
    return shutil.which("onex") or "onex"


def run_pinned_delegation(backend_id: str) -> tuple[int, str, str]:
    """Run one ``onex delegate`` pinned to ``backend_id``, from the home directory."""
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    try:
        done = subprocess.run(
            [
                _onex_executable(),
                "delegate",
                "--json",
                "--backend-id",
                backend_id,
                _TEST_PROMPT,
            ],
            capture_output=True,
            text=True,
            cwd=Path.home(),
            env=env,
            timeout=_TEST_TIMEOUT_S,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return 124, "", f"onex delegate failed: no answer within {_TEST_TIMEOUT_S}s"
    except OSError as error:
        return 127, "", f"onex delegate failed: could not run onex ({error})"
    return done.returncode, done.stdout, done.stderr


def _failure_reason(stdout: str, stderr: str, exit_code: int) -> str:
    for line in reversed(stderr.splitlines()):
        if line.startswith("onex delegate failed: "):
            reason = line.removeprefix("onex delegate failed: ")
            return reason.split(" (run ", 1)[0].strip()
    for line in reversed(stdout.splitlines()):
        if not line.startswith("{"):
            continue
        try:
            attempts = (json.loads(line).get("result") or {}).get("attempts") or []
        except ValueError:
            continue
        for attempt in reversed(attempts):
            if attempt.get("error_message"):
                return str(attempt["error_message"])[:300]
    return f"the test delegation exited with status {exit_code}"


def _receipt(stderr: str) -> dict[str, object]:
    for token in reversed(stderr.split()):
        if token.endswith("/receipt.json"):
            try:
                data = json.loads(Path(token).read_text(encoding="utf-8"))
            except (OSError, ValueError):
                return {}
            return data if isinstance(data, dict) else {}
    return {}


def _text(value: object) -> str | None:
    return str(value) if value else None


class HandlerModelSetup:
    """Status and pinned test delegations for the models delegation can use."""

    def __init__(
        self,
        run_delegation: DelegateRunner | None = None,
        write_model_list: ModelListWriter | None = None,
    ) -> None:
        self._run_delegation = run_delegation or run_pinned_delegation
        self._write_model_list = write_model_list

    def handle(self, request: ModelModelSetupRequest) -> ModelModelSetupResult:
        if request.operation == "status":
            return ModelModelSetupResult(
                operation="status",
                providers=tuple(self._status(p) for p in PROVIDERS),
            )
        if request.operation in ("add_models", "reorder", "remove_model"):
            return self._edit_models(request)
        if request.operation == "default_model":
            return self._default_model(request)
        if request.operation == "forget":
            if request.provider is not None:
                self._forget(request.provider)
            return ModelModelSetupResult(operation="forget")
        providers = (
            (request.provider,)
            if request.provider is not None
            else tuple(p for p in PROVIDERS if self.is_set_up(p))
        )
        return ModelModelSetupResult(
            operation="test", tests=tuple(self._test(p) for p in providers)
        )

    # -- the model onboarding registers ------------------------------------

    def _default_model(self, request: ModelModelSetupRequest) -> ModelModelSetupResult:
        """The provider's default model: environment, overlay, else the contract's; stores nothing."""
        provider = request.provider
        if provider not in _DEFAULT_MODEL_ENV:
            raise ModelSetupRefusedError(
                "only a provider where you choose the model has a default declared "
                f"({', '.join(sorted(_DEFAULT_MODEL_ENV))}); name one of them"
            )
        setting = f"{provider}_default_model"
        try:
            model = resolve_setting(
                DEFAULT_MODEL_NODE,
                setting,
                fields=_DEFAULT_MODEL_FIELDS,
                env_vars=(_DEFAULT_MODEL_ENV[provider],),
            )
        except NodeOverlayError as error:
            refusal = ModelSetupRefusedError(
                f"{error}. To choose a model yourself: onex models add {provider} "
                "--model <model id>"
            )
            # Only an absent value falls back to the packaged default: an overlay
            # that is present but broken is refused, never read as absent.
            try:
                load_node_overlay(DEFAULT_MODEL_NODE, fields=_DEFAULT_MODEL_FIELDS)
            except NodeOverlayError:
                raise refusal from None
            model = packaged_default(setting)
            if not model:
                raise refusal from None
        return ModelModelSetupResult(operation="default_model", models=(model,))

    # -- the ordered models of a stored key --------------------------------

    def _edit_models(self, request: ModelModelSetupRequest) -> ModelModelSetupResult:
        """Compute the key's next list and hand the write to the local secret store."""
        provider = request.provider
        if provider is None:
            raise ModelSetupRefusedError(
                "name a provider that holds a key: gemini, openrouter, openai or "
                "anthropic"
            )
        stored = resolve_local_byok_credential_models(provider)
        if provider not in registered_local_byok_providers():
            raise ModelSetupRefusedError(
                f"no {provider} key is stored; add one with: onex models add {provider}"
            )
        asked = request.models
        if not asked:
            raise ModelSetupRefusedError("name at least one model")
        if len(set(asked)) != len(asked):
            raise ModelSetupRefusedError("a model is named twice")
        if request.operation == "add_models":
            already = [model for model in asked if model in stored]
            if already:
                raise ModelSetupRefusedError(
                    f"{', '.join(already)} is already in your {provider} list: "
                    f"{', '.join(stored)}"
                )
            wanted = (*stored, *asked)
        elif request.operation == "reorder":
            if set(asked) != set(stored):
                raise ModelSetupRefusedError(
                    "reorder names exactly the models your "
                    f"{provider} list holds ({', '.join(stored) or 'none'}), "
                    "each once; add or remove models with their own commands"
                )
            wanted = asked
        else:
            missing = [model for model in asked if model not in stored]
            if missing:
                raise ModelSetupRefusedError(
                    f"{', '.join(missing)} is not in your {provider} list: "
                    f"{', '.join(stored) or 'empty'}"
                )
            wanted = tuple(model for model in stored if model not in asked)
            if not wanted:
                raise ModelSetupRefusedError(
                    f"that would leave your {provider} key with no model; add the "
                    f"one you want first, or delete the key with: onex models "
                    f"remove {provider}"
                )
        if self._write_model_list is None:
            raise ModelSetupRefusedError(
                "no writer for the model list was given; the list is stored by "
                "the local secret store, which the onex models command passes in"
            )
        return ModelModelSetupResult(
            operation=request.operation,
            models=self._write_model_list(provider, wanted),
        )

    # -- where things live -------------------------------------------------

    @staticmethod
    def _state_dir() -> Path:
        return default_evidence_db_path().parent

    def _results_path(self) -> Path:
        return self._state_dir() / "model_tests.json"

    def overrides_path(self) -> Path:
        """The local routes file onboarding writes when it sets up Ollama."""
        return self._state_dir() / "bifrost_overrides.yaml"

    # -- set up? -----------------------------------------------------------

    def is_set_up(self, provider: ModelProvider) -> bool:
        if provider == "ollama":
            return self._ollama_routes_configured()
        return provider in registered_local_byok_providers()

    def _ollama_routes_configured(self) -> bool:
        try:
            config = yaml.safe_load(_MODEL_CONFIG.read_text(encoding="utf-8")) or {}
            port = (config.get("ollama") or {}).get("port")
            overrides = (
                yaml.safe_load(self.overrides_path().read_text(encoding="utf-8")) or {}
            )
        except (OSError, yaml.YAMLError):
            return False
        if not port:
            return False
        return any(
            isinstance(backend, dict)
            and backend.get("backend_id") == OLLAMA_BACKEND
            and f":{port}/" in str(backend.get("endpoint_url", ""))
            for backend in overrides.get("backends") or []
        )

    @staticmethod
    def backend_for(provider: ModelProvider) -> str | None:
        if provider == "ollama":
            return OLLAMA_BACKEND
        backend = resolve_byok_provider_backend(provider)
        return backend.backend_id if backend is not None else None

    # -- the recorded results ----------------------------------------------

    def _read_results(self) -> dict[str, object]:
        try:
            data = json.loads(self._results_path().read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def _write_results(self, results: dict[str, object]) -> None:
        path = self._results_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(results, indent=2, sort_keys=True), encoding="utf-8")
        tmp.replace(path)

    def _forget(self, provider: ModelProvider) -> None:
        results = self._read_results()
        if results.pop(provider, None) is not None:
            self._write_results(results)

    def _status(self, provider: ModelProvider) -> ModelModelStatus:
        set_up = self.is_set_up(provider)
        recorded = self._read_results().get(provider) if set_up else None
        last = None
        if isinstance(recorded, dict):
            try:
                last = ModelModelTestResult.model_validate(recorded)
            except ValueError:
                last = None
        models = resolve_local_byok_credential_models(provider) if set_up else ()
        return ModelModelStatus(
            provider=provider, set_up=set_up, last_test=last, models=models
        )

    # -- the test ----------------------------------------------------------

    @staticmethod
    def _chosen_model(provider: ModelProvider) -> str | None:
        """The model the customer chose for ``provider``'s key, if its row asks them.

        OMN-20844. ``None`` for a provider whose catalogue row picks the model
        (the stored model there can move on a re-aim) and for no stored model.
        """
        row = resolve_byok_provider_backend(provider)
        if row is None or not row.customer_chooses_model:
            return None
        return resolve_local_byok_credential_model(provider)

    def _test(self, provider: ModelProvider) -> ModelModelTestResult:
        now = datetime.now(UTC).isoformat(timespec="seconds")
        backend_id = self.backend_for(provider)
        if backend_id is None:
            return ModelModelTestResult(
                provider=provider,
                status="failed",
                tested_at=now,
                reason=f"this onex does not route a {provider} key yet",
            )
        if not self.is_set_up(provider):
            return ModelModelTestResult(
                provider=provider, status="not_set_up", tested_at=now
            )
        exit_code, stdout, stderr = self._run_delegation(backend_id)
        receipt = _receipt(stderr)
        answered = receipt.get("backend_id")
        chosen = self._chosen_model(provider)
        answered_model = _text(receipt.get("model"))
        if (
            exit_code == 0
            and receipt.get("status") == "success"
            and answered == backend_id
            and chosen is not None
            and answered_model != chosen
        ):
            # OMN-20844: the customer chose this model, so an answer on any
            # other model is not a pass for their choice.
            result = ModelModelTestResult(
                provider=provider,
                status="failed",
                tested_at=now,
                backend_id=backend_id,
                reason=f"answered on {answered_model}, not your chosen model {chosen}",
            )
        elif (
            exit_code == 0
            and receipt.get("status") == "success"
            and answered == backend_id
        ):
            result = ModelModelTestResult(
                provider=provider,
                status="passed",
                tested_at=now,
                backend_id=backend_id,
                model=_text(receipt.get("model")),
                endpoint=_text(receipt.get("endpoint")),
            )
        else:
            reason = _failure_reason(stdout, stderr, exit_code)
            if exit_code == 0 and answered not in (None, backend_id):
                reason = f"answered by {answered}, not {backend_id}"
            result = ModelModelTestResult(
                provider=provider,
                status="failed",
                tested_at=now,
                backend_id=backend_id,
                reason=reason,
            )
        results = self._read_results()
        results[provider] = result.model_dump(mode="json")
        self._write_results(results)
        return result


__all__ = [
    "OLLAMA_BACKEND",
    "PROVIDERS",
    "DelegateRunner",
    "HandlerModelSetup",
    "key_looks_like",
    "run_pinned_delegation",
]
