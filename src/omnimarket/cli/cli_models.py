# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""``onex models`` — set up the models delegation can use, one provider at a time.

    onex models add openai          # hidden key prompt, store, one test delegation
    onex models list                # each provider: set up or not, last test result
    onex models test [gemini]       # re-run the test delegation
    onex models remove openrouter   # delete the stored key

A developer can use any combination of Gemini, OpenRouter, OpenAI and Ollama.
Delegation chooses among the ones set up; nothing here picks a model for a run.
Onboarding calls ``add`` once for each model the developer chose, so a model
added later is set up exactly as one chosen at onboarding.

WHAT ``add`` DOES
    The key is stored by the same code as ``onex secret set
    llm.<provider>.api_key`` (plan and model resolved, route key registered),
    then one delegation is run pinned to that provider's backend
    (``onex delegate --backend-id``). The pin is pin-or-refuse: a run answered
    by any other backend fails, so a pass proves this key answered.

OLLAMA
    Ollama needs no key; onboarding installs it, downloads a model and writes
    the local routes. ``add ollama`` and ``test ollama`` test those routes when
    they exist, and otherwise say how to set Ollama up.

The key is read from stdin when piped and from a hidden prompt on a terminal,
never from an argument (see ``onex secret``), and is never echoed.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from getpass import getpass
from pathlib import Path

import click
import yaml

from omnimarket.cli.cli_secret import delete_secret_value, store_secret_value
from omnimarket.inference.local_byok_credential_adapter import (
    registered_local_byok_providers,
)
from omnimarket.projection.sqlite_database import default_evidence_db_path
from omnimarket.routing.byok_provider_backends import resolve_byok_provider_backend

__all__ = ["key_looks_like", "models_group"]

#: The models a developer chooses from, in the order onboarding lists them.
PROVIDERS: tuple[str, ...] = ("gemini", "openrouter", "openai", "ollama")
KEYED_PROVIDERS: tuple[str, ...] = ("gemini", "openrouter", "openai")

LABELS: dict[str, str] = {
    "gemini": "Gemini",
    "openrouter": "OpenRouter",
    "openai": "OpenAI",
    "ollama": "Ollama",
    "anthropic": "Anthropic (Claude)",
}
KEY_PAGES: dict[str, str] = {
    "gemini": "aistudio.google.com/apikey",
    "openrouter": "openrouter.ai/keys",
    "openai": "platform.openai.com/api-keys",
}

#: The backend Ollama's routes replace; onboarding points it at Ollama.
OLLAMA_BACKEND = "local-coder"

#: Leading characters of each provider's keys, most specific first: an
#: OpenRouter or Anthropic key also starts with ``sk-``.
_KEY_PREFIXES: tuple[tuple[str, str], ...] = (
    ("sk-or-", "openrouter"),
    ("sk-ant-", "anthropic"),
    ("AIza", "gemini"),
    ("sk-", "openai"),
)

_TEST_PROMPT = "Reply with exactly one word: hello"
_TEST_TIMEOUT_S = 300
_OLLAMA_CONFIG = Path(__file__).resolve().parents[1] / "configs/bifrost_delegation.yaml"


def key_looks_like(value: str) -> str | None:
    """The provider whose keys start like ``value``, or ``None`` when unknown."""
    for prefix, provider in _KEY_PREFIXES:
        if value.startswith(prefix):
            return provider
    return None


@dataclass(frozen=True)
class ModelTestResult:
    """One test delegation pinned to a provider's backend."""

    provider: str
    status: str  # "passed" | "failed" | "not_set_up"
    tested_at: str
    backend_id: str | None = None
    model: str | None = None
    endpoint: str | None = None
    reason: str | None = None


def _state_dir() -> Path:
    return default_evidence_db_path().parent


def _results_path() -> Path:
    return _state_dir() / "model_tests.json"


def _overrides_path() -> Path:
    return _state_dir() / "bifrost_overrides.yaml"


def _read_results() -> dict[str, dict[str, object]]:
    try:
        data = json.loads(_results_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _record(result: ModelTestResult) -> None:
    results = _read_results()
    results[result.provider] = asdict(result)
    path = _results_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(results, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(path)


def _forget(provider: str) -> None:
    results = _read_results()
    if results.pop(provider, None) is not None:
        _results_path().write_text(
            json.dumps(results, indent=2, sort_keys=True), encoding="utf-8"
        )


def _ollama_port() -> int | None:
    try:
        block = (yaml.safe_load(_OLLAMA_CONFIG.read_text(encoding="utf-8")) or {}).get(
            "ollama"
        ) or {}
    except (OSError, yaml.YAMLError):
        return None
    port = block.get("port")
    return int(port) if port else None


def ollama_routes_configured() -> bool:
    """Whether this machine's local routes send the local backend to Ollama."""
    port = _ollama_port()
    if port is None:
        return False
    try:
        overrides = yaml.safe_load(_overrides_path().read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        return False
    for backend in overrides.get("backends") or []:
        if not isinstance(backend, dict):
            continue
        if backend.get("backend_id") == OLLAMA_BACKEND and f":{port}/" in str(
            backend.get("endpoint_url", "")
        ):
            return True
    return False


def is_set_up(provider: str) -> bool:
    if provider == "ollama":
        return ollama_routes_configured()
    return provider in registered_local_byok_providers()


def backend_for(provider: str) -> str:
    if provider == "ollama":
        return OLLAMA_BACKEND
    backend = resolve_byok_provider_backend(provider)
    if backend is None:  # every keyed provider here is in the catalogue
        raise click.ClickException(f"onex does not route a {provider} key yet.")
    return backend.backend_id


def _onex_executable() -> str:
    beside = Path(sys.executable).with_name("onex")
    if beside.is_file():
        return str(beside)
    found = shutil.which("onex")
    if found is None:
        raise click.ClickException(
            "could not find the onex command to run a test delegation."
        )
    return found


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


def run_test_delegation(backend_id: str) -> tuple[int, str, str]:
    """Run one delegation pinned to ``backend_id``: (exit code, stdout, stderr)."""
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
    return done.returncode, done.stdout, done.stderr


def test_provider(provider: str) -> ModelTestResult:
    """Test ``provider`` with one pinned delegation and record the result."""
    now = datetime.now(UTC).isoformat(timespec="seconds")
    if not is_set_up(provider):
        return ModelTestResult(provider=provider, status="not_set_up", tested_at=now)
    backend_id = backend_for(provider)
    exit_code, stdout, stderr = run_test_delegation(backend_id)
    receipt = _receipt(stderr)
    answered = receipt.get("backend_id")
    if exit_code == 0 and receipt.get("status") == "success" and answered == backend_id:
        result = ModelTestResult(
            provider=provider,
            status="passed",
            tested_at=now,
            backend_id=backend_id,
            model=str(receipt.get("model") or "") or None,
            endpoint=str(receipt.get("endpoint") or "") or None,
        )
    else:
        reason = _failure_reason(stdout, stderr, exit_code)
        if exit_code == 0 and answered not in (None, backend_id):
            reason = f"answered by {answered}, not {backend_id}"
        result = ModelTestResult(
            provider=provider,
            status="failed",
            tested_at=now,
            backend_id=backend_id,
            reason=reason,
        )
    _record(result)
    return result


def _show(result: ModelTestResult, as_json: bool) -> None:
    if as_json:
        click.echo(json.dumps(asdict(result)))
        return
    label = LABELS[result.provider]
    if result.status == "passed":
        click.echo(f"{label}  ✓ answered ({result.model or result.backend_id})")
    elif result.status == "failed":
        click.echo(f"{label}  ✗ {result.reason}")
        if result.provider in KEYED_PROVIDERS:
            click.echo(f"  Fix it, then run: onex models test {result.provider}")
    else:
        click.echo(f"{label}  not set up. {_how_to_add(result.provider)}")


def _how_to_add(provider: str) -> str:
    if provider == "ollama":
        return (
            "Ollama is installed by onboarding, which downloads a model sized to "
            "this Mac: run onboarding again with --provider ollama."
        )
    return f"Add it with: onex models add {provider}"


def _read_key(provider: str) -> str:
    """The key from stdin when piped, else a hidden prompt. Never echoed."""
    label = LABELS[provider]
    if sys.stdin is not None and sys.stdin.isatty():
        value = getpass(
            f"Paste your {label} API key ({KEY_PAGES[provider]}; input is hidden): "
        ).strip()
    else:
        value = sys.stdin.read().strip()
    if not value:
        raise click.ClickException(f"no {label} key was entered; nothing was stored.")
    looks = key_looks_like(value)
    if looks is not None and looks != provider:
        raise click.ClickException(
            f"that looks like a key for {LABELS[looks]}, not {label}. Nothing was "
            "stored."
        )
    return value


_provider_arg = click.argument(
    "provider", type=click.Choice(PROVIDERS, case_sensitive=False)
)
_json_opt = click.option(
    "--json", "as_json", is_flag=True, help="One JSON object per line."
)


@click.group("models")
def models_group() -> None:  # stub-ok: a click group's body IS its subcommands
    """Set up the models delegation can use: Gemini, OpenRouter, OpenAI, Ollama.

    Set up any combination; delegation chooses among them for each task.
    """


@models_group.command("add")
@_provider_arg
@_json_opt
def add_model(provider: str, as_json: bool) -> None:
    """Store PROVIDER's key, then test it with one delegation.

    The key is read from stdin when piped, or asked for at a hidden prompt.
    A key already stored for PROVIDER is replaced.
    """
    provider = provider.lower()
    if provider == "ollama":
        if not ollama_routes_configured():
            raise click.ClickException(_how_to_add("ollama"))
    else:
        value = _read_key(provider)
        store_secret_value(f"llm.{provider}.api_key", value, force=True)
    result = test_provider(provider)
    _show(result, as_json)
    if result.status != "passed":
        sys.exit(1)


@models_group.command("test")
@click.argument(
    "provider", required=False, type=click.Choice(PROVIDERS, case_sensitive=False)
)
@_json_opt
def test_models(provider: str | None, as_json: bool) -> None:
    """Test PROVIDER (or every set-up provider) with one delegation each."""
    providers = (
        [provider.lower()] if provider else [p for p in PROVIDERS if is_set_up(p)]
    )
    if not providers:
        raise click.ClickException(
            "no model is set up. Add one with: onex models add <provider>"
        )
    failed = False
    for name in providers:
        result = test_provider(name)
        _show(result, as_json)
        failed = failed or result.status != "passed"
    if failed:
        sys.exit(1)


@models_group.command("list")
@_json_opt
def list_models(as_json: bool) -> None:
    """Each provider: set up or not, and its last test result."""
    results = _read_results()
    for provider in PROVIDERS:
        set_up = is_set_up(provider)
        last = results.get(provider) if set_up else None
        if as_json:
            click.echo(
                json.dumps({"provider": provider, "set_up": set_up, "last_test": last})
            )
            continue
        label = f"{LABELS[provider]:<11}"
        if not set_up:
            click.echo(f"{label} not set up")
        elif last is None:
            click.echo(f"{label} set up, not tested yet")
        elif last.get("status") == "passed":
            click.echo(
                f"{label} set up, last test passed {last.get('tested_at')} ({last.get('model')})"
            )
        else:
            click.echo(
                f"{label} set up, last test FAILED {last.get('tested_at')}: {last.get('reason')}"
            )


@models_group.command("remove")
@_provider_arg
def remove_model(provider: str) -> None:
    """Delete PROVIDER's stored key so delegation no longer uses it."""
    provider = provider.lower()
    if provider == "ollama":
        raise click.ClickException(
            f"Ollama's routes are in {_overrides_path()}; delete that file to stop "
            "using Ollama, and quit the Ollama app."
        )
    if not is_set_up(provider):
        raise click.ClickException(f"no {LABELS[provider]} key is stored.")
    delete_secret_value(f"llm.{provider}.api_key")
    _forget(provider)
