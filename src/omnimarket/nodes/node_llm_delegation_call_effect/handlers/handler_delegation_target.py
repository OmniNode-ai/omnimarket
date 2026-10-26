# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Target adapters owned by the existing delegation effect.

No adapter is a node. Model calls compose the public model router and generic
inference effect. Coding sessions run locally in an exact-commit worktree;
runtime routing decides which host receives the command.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import os
import re
import shutil
import signal
import time
from importlib import resources
from pathlib import Path
from typing import Any, Literal

import yaml
from omnibase_core.models.routing.model_routing_policy import ModelRoutingPolicy

from omnimarket.inference.secret_store_resolver import resolve_api_key_async
from omnimarket.models.delegation.model_delegation_target import (
    ModelDelegationGeneration,
    ModelDelegationTargetRequest,
    ModelDelegationTargetResult,
)
from omnimarket.nodes.node_model_router import HandlerModelRouter, ModelRoutingRequest


def load_delegation_config() -> dict[str, Any]:
    """Read contract defaults and the deployment's explicit adapter overlay."""
    contract = yaml.safe_load(
        resources.files("omnimarket.nodes.node_llm_delegation_call_effect")
        .joinpath("contract.yaml")
        .read_text()
    )
    config: dict[str, Any] = contract["delegation_adapters"]
    path = os.environ.get("DELEGATION_ADAPTER_CONFIG_PATH")
    if path:
        overlay = yaml.safe_load(Path(path).read_text())
        if not isinstance(overlay, dict):
            raise ValueError("delegation adapter overlay must be a mapping")
        config = config | overlay
    return config


class HandlerClaudeCodeArguments:
    """Claude Code session; the agent retains its permission checks."""

    def argv(self, request: ModelDelegationTargetRequest, *, model: str) -> list[str]:
        return [
            "claude",
            "-p",
            "--permission-mode",
            "acceptEdits",
            "--output-format",
            "json",
            "--model",
            model,
        ]


class HandlerClaudeGlmArguments(HandlerClaudeCodeArguments):
    """GLM through Claude Code, with deployment-bound environment references."""


class HandlerCodexArguments:
    """Codex session; the host account supplies its configured model."""

    def argv(self, request: ModelDelegationTargetRequest, *, model: str) -> list[str]:
        return ["codex", "exec", "-s", "workspace-write", "--json", "-"]


class HandlerAntigravityArguments:
    """Antigravity session using the host's signed-in account."""

    def argv(self, request: ModelDelegationTargetRequest, *, model: str) -> list[str]:
        argv = [
            "agy",
            "-p",
            request.brief,
            "--output-format",
            "text",
            "--mode",
            "accept-edits",
        ]
        if model:
            argv.extend(["--model", model])
        return argv


class AntigravityRefusalError(ValueError):
    """A refusal to run Antigravity with a stable, non-secret code."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code: str = code


class HandlerDelegationModelCall:
    """Route once, validate parameter evidence, invoke generic inference."""

    def __init__(self, *, router: Any, inference: Any) -> None:
        self._router = router
        self._inference = inference

    async def execute(
        self,
        request: ModelDelegationTargetRequest,
        *,
        generation: ModelDelegationGeneration,
        route: dict[str, Any],
        timeout_seconds: float,
    ) -> ModelDelegationTargetResult:
        selected = await self._router.handle_async(
            ModelRoutingRequest(
                prompt=request.brief,
                role=request.call_site,
                correlation_id=str(request.request_id),
            )
        )
        # Fallbacks must use the selected model's capability evidence and key.
        model = route["models"][selected.model_key] if "models" in route else route
        supplied = generation.model_dump(exclude_none=True)
        statuses = model.get("parameter_status", {})
        supported = (
            {name for name, status in statuses.items() if status == "honored"}
            if statuses
            else set(model.get("supported_parameters", ()))
        )
        refused = sorted(set(supplied) - supported)
        if refused:
            raise ValueError(
                f"route has no honored capability evidence for: {', '.join(refused)}"
            )

        from omnibase_infra.enums import EnumLlmOperationType
        from omnibase_infra.nodes.node_llm_inference_effect.models import (
            ModelLlmInferenceRequest,
        )

        mappings = model.get("parameter_mappings", {})
        reserved = {
            "model",
            "messages",
            "prompt",
            "stream",
            "api_key",
            "endpoint_url",
            "base_url",
        }
        typed: dict[str, Any] = {}
        extra: dict[str, Any] = {}
        for name, value in supplied.items():
            wire_name = mappings.get(name, name)
            if wire_name in reserved:
                raise ValueError(
                    "parameter mapping attempts to replace routing authority"
                )
            if (
                wire_name != name
                and wire_name in ModelDelegationGeneration.model_fields
            ):
                raise ValueError(
                    "parameter mapping replaces another generation parameter"
                )
            if wire_name in typed or wire_name in extra:
                raise ValueError("parameter mappings collide on one wire field")
            if name == wire_name and name in {
                "temperature",
                "top_p",
                "max_tokens",
                "stop",
            }:
                typed[name] = value
            else:
                extra[wire_name] = value
        key_ref = model.get("api_key_ref")
        key = await resolve_api_key_async(key_ref, required=True) if key_ref else None
        sent = ModelLlmInferenceRequest(
            base_url=selected.endpoint_url,
            endpoint_url=selected.endpoint_url,
            operation_type=EnumLlmOperationType.CHAT_COMPLETION,
            model=model["model_name"],
            messages=({"role": "user", "content": request.brief},),
            timeout_seconds=timeout_seconds,
            max_retries=0,
            api_key=key,
            extra_body=extra,
            **typed,
        )
        response = await self._inference.handle(sent, correlation_id=request.request_id)
        if getattr(response, "truncated", False):
            raise ValueError("inference output truncated by the requested token budget")
        return ModelDelegationTargetResult(
            request_id=request.request_id,
            target=request.target,
            status="completed",
            content=response.generated_text or "",
            model_used=getattr(response, "model_used", model["model_name"]),
            tool_calls=tuple(
                call.model_dump(mode="json") for call in response.tool_calls
            ),
        )


def _agent_environment() -> dict[str, str]:
    """A child checkout must not inherit the launching hook's git location."""
    return {
        key: value
        for key, value in os.environ.items()
        if key
        not in {
            "GIT_DIR",
            "GIT_WORK_TREE",
            "GIT_INDEX_FILE",
            "GIT_COMMON_DIR",
            "PYTHONPATH",
            "CLAUDECODE",
        }
    }


def _antigravity_environment(env: dict[str, str]) -> dict[str, str]:
    """Keep Antigravity on the host login instead of key or ADC credentials."""
    return {
        key: value
        for key, value in env.items()
        if key
        not in {
            "GEMINI_API_KEY",
            "GOOGLE_API_KEY",
            "ANTIGRAVITY_API_KEY",
            "AGY_ADC_AUTH",
            "GOOGLE_APPLICATION_CREDENTIALS",
        }
    }


async def _git(*argv: str, cwd: Path, env: dict[str, str]) -> str:
    process = await asyncio.create_subprocess_exec(
        "git",
        *argv,
        cwd=cwd,
        env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, _stderr = await asyncio.wait_for(process.communicate(), timeout=60)
    except BaseException:
        if process.returncode is None:
            process.kill()
        await process.wait()
        raise
    if process.returncode:
        # Do not copy git credential-helper output into a durable bus receipt.
        raise RuntimeError(f"local git {argv[0]} failed (exit {process.returncode})")
    return stdout.decode().strip()


def _read_agent_result(path: Path, target: str) -> tuple[str, str | None, bool]:
    content, session_id, failed = "", None, False
    # The full stream remains at output_path, while bus receipts stay bounded.
    with path.open() as stream:
        for line in stream:
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(record, dict):
                continue
            if target == "codex":
                session_id = record.get("thread_id", session_id)
                failed = failed or record.get("type") in {"turn.failed", "error"}
                item = record.get("item", {})
                if (
                    record.get("type") == "item.completed"
                    and item.get("type") == "agent_message"
                ):
                    content = str(item.get("text", ""))
            elif record.get("type") == "result":
                session_id = record.get("session_id", session_id)
                content = str(record.get("result", ""))
                failed = bool(record.get("is_error", False))
    return content[-262144:], session_id, failed


class HandlerDelegationTarget:
    """Canonical typed handler for model calls and local coding-agent sessions."""

    def __init__(self, event_bus: Any, config: dict[str, Any] | None = None) -> None:
        self._config = config if config is not None else load_delegation_config()
        self._bus = event_bus

    @property
    def handler_type(self) -> Literal["NODE_HANDLER"]:
        return "NODE_HANDLER"

    @property
    def handler_category(self) -> Literal["EFFECT"]:
        return "EFFECT"

    async def handle(
        self, request: ModelDelegationTargetRequest
    ) -> ModelDelegationTargetResult:
        site = self._config.get("call_sites", {}).get(request.call_site)
        if site is None:
            raise ValueError(f"undeclared call_site: {request.call_site}")
        # The deployment allowlists contract locations; a request names only
        # the call site and can never choose a path or endpoint itself.
        if "contract_path" in site:
            caller = yaml.safe_load(Path(site["contract_path"]).read_text())
            site = caller["delegation_calls"][site["operation"]]
        elif request.target == "model-call":
            raise ValueError("model-call requires a declared caller contract_path")
        if request.target not in site["targets"]:
            raise ValueError("target is not declared by this call site's contract")
        timeout = float(site["timeout_seconds"])
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("call-site timeout_seconds must be finite and positive")
        if request.timeout_seconds is not None:
            timeout = min(timeout, request.timeout_seconds)
        if request.target == "model-call":
            return await self._model_call(request, site, timeout)
        return await self._agent_session(request, site, timeout)

    async def _model_call(
        self,
        request: ModelDelegationTargetRequest,
        site: dict[str, Any],
        timeout: float,
    ) -> ModelDelegationTargetResult:
        from omnibase_infra.mixins.mixin_llm_http_transport import MixinLlmHttpTransport
        from omnibase_infra.nodes.node_llm_inference_effect import (
            HandlerLlmOpenaiCompatible,
        )

        class Transport(MixinLlmHttpTransport):
            def __init__(self) -> None:
                self._init_llm_http_transport(target_name="delegation-target")

            async def close(self) -> None:
                await self._close_http_client()

        router = HandlerModelRouter(
            policy=ModelRoutingPolicy.model_validate(site["model_routing"]),
            registry=self._config["model_registry"],
            event_bus=self._bus,
        )
        generation = ModelDelegationGeneration.model_validate(
            site.get("generation", {})
        ).overridden_by(request.generation)
        transport = Transport()
        try:
            return await HandlerDelegationModelCall(
                router=router, inference=HandlerLlmOpenaiCompatible(transport=transport)
            ).execute(
                request,
                generation=generation,
                route={"models": self._config["model_registry"]},
                timeout_seconds=timeout,
            )
        finally:
            await transport.close()

    async def _agent_session(
        self,
        request: ModelDelegationTargetRequest,
        site: dict[str, Any],
        timeout: float,
    ) -> ModelDelegationTargetResult:
        assert request.repo is not None
        assert request.commit_sha is not None
        owner, name = request.repo.split("/")
        if owner not in self._config["allowed_owners"] or name in {".", ".."}:
            raise ValueError("repository is outside this runtime's delegation scope")
        root = Path(self._config["repository_root"]).resolve()
        clone = (root / name).resolve()
        if not clone.is_relative_to(root):
            raise ValueError("repository resolves outside the configured root")
        run_root = Path(self._config["work_root"]).resolve() / str(request.request_id)
        tree, output = run_root / "worktree", run_root / "output.jsonl"
        receipt_path = run_root / "receipt.json"
        digest = hashlib.sha256(request.model_dump_json().encode()).hexdigest()
        started = time.monotonic()
        result: ModelDelegationTargetResult
        env = _agent_environment()
        adapters: dict[
            str,
            HandlerClaudeCodeArguments
            | HandlerCodexArguments
            | HandlerAntigravityArguments,
        ] = {
            "codex": HandlerCodexArguments(),
            "claude-code": HandlerClaudeCodeArguments(),
            "claude-glm": HandlerClaudeGlmArguments(),
            "antigravity": HandlerAntigravityArguments(),
        }
        adapter = adapters[request.target]
        session_config = site.get("agents", {}).get(request.target, {})
        model = session_config.get("model", "")
        if request.agent_profile is not None and request.target != "codex":
            model = session_config.get("profiles", {}).get(request.agent_profile)
            if not model:
                raise ValueError(
                    "agent profile must be declared by the call-site contract"
                )
        if request.target not in {"codex", "antigravity"} and not model:
            raise ValueError("agent model must be supplied by the call-site contract")
        if request.target == "claude-glm" and not session_config.get("secret_env"):
            raise ValueError(
                "claude-glm requires deployment-bound credential references"
            )
        if request.target == "claude-glm":
            if not session_config.get("env", {}).get("ANTHROPIC_BASE_URL"):
                raise ValueError(
                    "claude-glm requires its configured Claude Code provider route"
                )
            env.pop("ANTHROPIC_API_KEY", None)
            env.pop("ANTHROPIC_AUTH_TOKEN", None)
        if request.target != "antigravity" and any(
            key.endswith(("_KEY", "_TOKEN", "_PASSWORD"))
            for key in session_config.get("env", {})
        ):
            raise ValueError("agent credentials must use secret_env references")
        if request.target != "antigravity":
            for key, ref in session_config.get("secret_env", {}).items():
                secret = await resolve_api_key_async(ref, required=True)
                if secret is None:
                    raise ValueError("agent credential reference did not resolve")
                env[key] = secret.get_secret_value()
            env.update(session_config.get("env", {}))
        else:
            env = _antigravity_environment(env)
        # mkdir is the local duplicate barrier. The lab-job store provides the
        # durable cross-runtime claim before this method is called. A completed
        # direct target request can return its recorded receipt on redelivery.
        try:
            run_root.mkdir(parents=True, exist_ok=False, mode=0o700)
        except FileExistsError:
            if receipt_path.is_file():
                saved = ModelDelegationTargetResult.model_validate_json(
                    receipt_path.read_text()
                )
                if saved.request_digest != digest:
                    raise ValueError(
                        "request_id already names a different delegation request"
                    ) from None
                return saved
            raise RuntimeError(
                "delegation request is already running or needs recovery"
            ) from None
        try:
            if request.target == "antigravity":
                if session_config.get("secret_env") or session_config.get("env"):
                    raise AntigravityRefusalError("credential-config")
                if len(request.brief) > 100_000:
                    raise AntigravityRefusalError("brief-too-large")
                if shutil.which("agy", path=env.get("PATH")) is None:
                    raise AntigravityRefusalError("not-installed")
            sha = await _git(
                "rev-parse",
                "--verify",
                f"{request.commit_sha}^{{commit}}",
                cwd=clone,
                env=env,
            )
            if sha != request.commit_sha:
                raise ValueError("resolved commit differs from requested commit")
            await _git(
                "worktree", "add", "--detach", str(tree), sha, cwd=clone, env=env
            )
            with output.open("wb") as stream:
                process = await asyncio.create_subprocess_exec(
                    *adapter.argv(request, model=model),
                    cwd=tree,
                    env=env,
                    stdin=asyncio.subprocess.DEVNULL
                    if request.target == "antigravity"
                    else asyncio.subprocess.PIPE,
                    stdout=stream,
                    stderr=asyncio.subprocess.STDOUT,
                    start_new_session=True,
                )
                timed_out = False
                try:
                    await asyncio.wait_for(
                        process.communicate()
                        if request.target == "antigravity"
                        else process.communicate(request.brief.encode()),
                        timeout=timeout,
                    )
                except (TimeoutError, asyncio.CancelledError):
                    if process.returncode is None:
                        os.killpg(process.pid, signal.SIGKILL)
                    await process.wait()
                    current = asyncio.current_task()
                    if current is not None and current.cancelling():
                        raise
                    timed_out = True
            if request.target == "antigravity":
                output_text = output.read_text()
                content = output_text[-262144:].strip()
                session_id: str | None = f"agy-{request.request_id}"
                failed = False
                if (process.returncode or not content) and re.search(
                    r"not logged in|log in|login|sign in|authenticat|unauthori[sz]ed",
                    output_text,
                    re.IGNORECASE,
                ):
                    raise AntigravityRefusalError("not-logged-in")
            else:
                content, session_id, failed = _read_agent_result(output, request.target)
            head = await _git("rev-parse", "HEAD", cwd=tree, env=env)
            result = ModelDelegationTargetResult(
                request_id=request.request_id,
                target=request.target,
                status="timed_out"
                if timed_out
                else (
                    "failed"
                    if process.returncode or failed or not session_id or not content
                    else "completed"
                ),
                content=content,
                exit_code=process.returncode,
                session_id=session_id,
                worktree=str(tree),
                commit_sha=sha,
                head_sha=head,
                output_path=str(output),
                receipt_path=str(receipt_path),
                duration_seconds=time.monotonic() - started,
                request_digest=digest,
            )
        except (OSError, RuntimeError, ValueError) as exc:
            result = ModelDelegationTargetResult(
                request_id=request.request_id,
                target=request.target,
                status="failed",
                commit_sha=request.commit_sha,
                worktree=str(tree),
                receipt_path=str(receipt_path),
                duration_seconds=time.monotonic() - started,
                detail=f"antigravity-refused: {exc.code}"
                if isinstance(exc, AntigravityRefusalError)
                else f"{type(exc).__name__}: session setup or execution failed",
                request_digest=digest,
            )
        temporary = receipt_path.with_suffix(".tmp")
        temporary.write_text(result.model_dump_json(indent=2))
        temporary.replace(receipt_path)
        return result


__all__ = [
    "HandlerAntigravityArguments",
    "HandlerClaudeCodeArguments",
    "HandlerClaudeGlmArguments",
    "HandlerCodexArguments",
    "HandlerDelegationModelCall",
    "HandlerDelegationTarget",
]
