# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Effect handler for node_github_repo_gateway_effect.

Resolves the GitHub token at the effect boundary, builds the real read
transport, and dispatches the requested read operation. Read-only: it performs
network reads and emits the typed result as its terminal event — no mutation of
GitHub state.
"""

from __future__ import annotations

import asyncio
import fcntl
import hashlib
import json
import os
import tempfile
import time
from pathlib import Path
from typing import BinaryIO, Literal, cast
from uuid import uuid4

from omnibase_core.models.dispatch.model_handler_output import ModelHandlerOutput

from omnimarket.config.state_root import resolve_onex_state_root
from omnimarket.github_landing.github_landing_transport import (
    MAX_LOG_READ_BYTES,
    JobLogWindow,
    _read_capped,
    _read_window,
)
from omnimarket.github_landing.model_github_http_exchange import (
    ModelGithubBytesResponse,
    ModelGithubHttpRequest,
    ModelGithubHttpResponse,
)
from omnimarket.handlers.node_overlay_reader import (
    find_node_overlay,
    load_structured_overlay,
)
from omnimarket.models.github_landing.model_transport_error import GitHubTransportError
from omnimarket.models.model_github_gateway_io import (
    DEFAULT_LOG_TAIL_BYTES,
    EnumGithubGatewayOperation,
    GithubGatewayResult,
    ModelGithubGatewayRequest,
    ModelGithubGatewayResponse,
    ModelJobLogFillResult,
    ModelJobLogTailResult,
)
from omnimarket.models.model_job_log_cache_miss import JOB_LOG_CACHE_MISS
from omnimarket.nodes.node_github_repo_gateway_effect.dispatcher import dispatch
from omnimarket.nodes.node_github_repo_gateway_effect.handlers.miss_fill_budget import (
    MissFillBudget,
    MissFillBudgetSpentError,
)
from omnimarket.nodes.node_github_repo_gateway_effect.models.model_job_log_cache import (
    ModelCachedJobLog,
    ModelJobLogCachePolicy,
)
from omnimarket.nodes.node_github_repo_gateway_effect.read_operations import (
    read_job_log_fill,
    read_job_log_tail,
)
from omnimarket.nodes.node_github_repo_gateway_effect.token_resolver import (
    resolve_github_token_async,
)
from omnimarket.nodes.node_github_repo_gateway_effect.transport import (
    GitHubReadTransportProtocol,
    RealGitHubReadTransport,
)

_HANDLER_ID = "node_github_repo_gateway_effect"
_JOB_LOG_OPERATIONS = frozenset(
    {EnumGithubGatewayOperation.JOB_LOG_TAIL, EnumGithubGatewayOperation.JOB_LOG_FILL}
)


def declared_cache_policy() -> ModelJobLogCachePolicy:
    """The policy as the overlay or the contract declares it, with no root resolved."""
    overlay_id = "github_job_logs"
    if find_node_overlay(overlay_id) is not None:
        return load_structured_overlay(overlay_id, ModelJobLogCachePolicy)
    import yaml

    contract = Path(__file__).parents[1] / "contract.yaml"
    return ModelJobLogCachePolicy.model_validate(
        yaml.safe_load(contract.read_text())["job_log_cache"]
    )


def lane_reads_cache_only() -> bool:
    """Whether a lane's ``job-log`` read answers from the cache only (declared, never coded)."""
    return declared_cache_policy().lane_reads_cache_only


def lane_job_log_flag() -> str | None:
    """The request flag a lane's ``job-log`` read carries, from the declared policy."""
    policy = declared_cache_policy()
    if not policy.lane_reads_cache_only:
        return None
    return "fill_on_miss" if policy.lane_miss_fill_max > 0 else "cache_only"


def cache_policy() -> ModelJobLogCachePolicy:
    """Use the existing host read-cache root, with an optional typed overlay."""
    policy = declared_cache_policy()
    root = policy.root or (
        resolve_onex_state_root(purpose="GitHub job-log cache")
        / "github-read-cache"
        / "job-logs"
    )
    return policy.model_copy(update={"root": root})


class _SpoolWindow(JobLogWindow):
    """The existing streaming transport feeds a private file instead of a RAM buffer."""

    def __init__(self, stream: BinaryIO) -> None:
        super().__init__(start=0, length=MAX_LOG_READ_BYTES)
        self.stream = stream
        self.digest = hashlib.sha256()

    def feed(self, chunk: bytes) -> None:
        self.stream.write(chunk)
        self.digest.update(chunk)

    def close(self, *, complete: bool) -> None:
        self.stream.flush()


class JobLogCacheTransport:
    """Job-scoped transport inside the existing gateway handler and event contract."""

    def __init__(
        self,
        root: Path,
        transport: GitHubReadTransportProtocol | None,
        *,
        ttl_seconds: int = 86400,
        miss_budget: MissFillBudget | None = None,
    ) -> None:
        self.root, self.transport, self.ttl_seconds = root, transport, ttl_seconds
        self.miss_budget = miss_budget
        self._miss_spent = False

    def handle(
        self, request: ModelGithubGatewayRequest
    ) -> ModelJobLogTailResult | ModelJobLogFillResult:
        if request.job_id is None:
            raise ValueError("job log requires job_id")
        self.request = request
        self._miss_spent = False
        # A fill wants the whole log in the shared cache, so it never settles for a runner's
        # partial copy of the window.
        legacy = (
            None
            if request.operation is EnumGithubGatewayOperation.JOB_LOG_FILL
            else self._legacy_view(request)
        )
        if legacy is not None:
            return legacy
        key = hashlib.sha256(
            f"{request.repo.lower()}:{request.job_id}".encode()
        ).hexdigest()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.raw = self.root / f"{key}.log"
        self.meta = self.root / f"{key}.meta.json"
        self.job = self.root / f"{key}.job.json"
        with (self.root / f"{key}.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            # The read function applies the same tail, offset and timestamp step
            # semantics to local bytes as it does to a live transport.
            read = (
                read_job_log_fill
                if request.operation is EnumGithubGatewayOperation.JOB_LOG_FILL
                else read_job_log_tail
            )
            return read(cast(GitHubReadTransportProtocol, self), request)

    def _legacy_view(
        self, request: ModelGithubGatewayRequest
    ) -> ModelJobLogTailResult | None:
        """Reuse a covered window from the runner's existing successful-read cache.

        A partial cached tail is never represented as a complete log. Unsupported
        steps and uncovered bytes remain misses. Replacement characters make byte
        coordinates ambiguous, so those fragments cannot supply a new window.
        """
        if request.step is not None:
            return None
        for path in self.root.parent.glob("*.json"):
            try:
                if not 0 <= time.time() - path.stat().st_mtime < self.ttl_seconds:
                    continue
                envelope = json.loads(json.loads(path.read_text())["stdout"])
                if (
                    envelope.get("outcome") != "completed"
                    or envelope.get("verb") != "job-log"
                ):
                    continue
                saved = ModelJobLogTailResult.model_validate(envelope["result"])
                if (saved.repo.lower(), saved.job_id) != (
                    request.repo.lower(),
                    request.job_id,
                ):
                    continue
                if saved.read_ceiling_hit or "\ufffd" in saved.text:
                    continue
                data = saved.text.encode()
                start = request.offset_bytes
                length = request.length_bytes or DEFAULT_LOG_TAIL_BYTES
                if start is None:
                    length = request.tail_bytes
                    start = max(0, saved.total_bytes - length)
                start = min(start, saved.total_bytes)
                end = min(start + length, saved.total_bytes)
                if (
                    not saved.start_offset
                    <= start
                    <= end
                    <= saved.start_offset + len(data)
                ):
                    continue
                text = data[
                    start - saved.start_offset : end - saved.start_offset
                ].decode()
                return ModelJobLogTailResult(
                    repo=request.repo,
                    job_id=saved.job_id,
                    total_bytes=saved.total_bytes,
                    text=text,
                    start_offset=start,
                    truncated=start > 0 or end < saved.total_bytes,
                    next_offset=end
                    if request.offset_bytes is not None and end < saved.total_bytes
                    else None,
                )
            except (OSError, ValueError, KeyError, TypeError):
                continue
        return None

    def _network(self) -> GitHubReadTransportProtocol:
        if self.request.cache_only or self.transport is None:
            raise GitHubTransportError(
                f"{JOB_LOG_CACHE_MISS}: no GitHub call made; job {self.request.job_id} "
                f"of {self.request.repo} is not under the job-log cache path "
                f"{self.root} (entry {self.raw.name}); node_job_log_filler_effect "
                f"fills that cache from merge-relevant runs"
            )
        if (
            self.request.fill_on_miss
            and self.miss_budget is not None
            and not self._miss_spent
        ):
            try:
                self.miss_budget.spend()
            except MissFillBudgetSpentError:
                raise GitHubTransportError(
                    f"{JOB_LOG_CACHE_MISS}: no GitHub call made; the lane miss-fill "
                    f"budget ({self.miss_budget.max_fills} fills per "
                    f"{self.miss_budget.window_seconds} s) is spent; job "
                    f"{self.request.job_id} of {self.request.repo} is not under "
                    f"the job-log cache path {self.root} (entry {self.raw.name}); "
                    f"node_job_log_filler_effect fills that cache from merge-relevant runs"
                ) from None
            self._miss_spent = True
        return self.transport

    def _metadata(self) -> ModelCachedJobLog | None:
        try:
            age = time.time() - self.meta.stat().st_mtime
            if not 0 <= age < self.ttl_seconds:
                return None
            meta = ModelCachedJobLog.model_validate_json(self.meta.read_text())
            if (
                meta.repo.lower() != self.request.repo.lower()
                or meta.job_id != self.request.job_id
                or meta.total_bytes != self.raw.stat().st_size
                or meta.total_bytes > MAX_LOG_READ_BYTES
            ):
                return None
            with self.raw.open("rb") as stream:
                if hashlib.file_digest(stream, "sha256").hexdigest() != meta.sha256:
                    return None
            return meta
        except (OSError, ValueError):
            return None

    def _log(self, request: ModelGithubHttpRequest) -> ModelCachedJobLog:
        meta = self._metadata()
        if meta is not None:
            return meta
        transport = self._network()
        fd, name = tempfile.mkstemp(dir=self.root, suffix=".tmp")
        temporary = Path(name)
        try:
            with os.fdopen(fd, "wb") as stream:
                window = _SpoolWindow(stream)
                response = transport.send_bytes_window_sync(request, window=window)
                if response.status != 200 or response.over_limit:
                    raise GitHubTransportError(
                        f"job log not cached: HTTP {response.status}, "
                        f"read_ceiling_hit={response.over_limit}"
                    )
                if stream.tell() != response.total_bytes:
                    raise GitHubTransportError("job log stream was incomplete")
                os.fsync(stream.fileno())
            meta = ModelCachedJobLog(
                repo=self.request.repo,
                job_id=cast(int, self.request.job_id),
                total_bytes=response.total_bytes,
                sha256=window.digest.hexdigest(),
                # Only quota readings survive; no redirect or authentication headers.
                headers={
                    k.lower(): v
                    for k, v in response.headers.items()
                    if k.lower().startswith("x-ratelimit-")
                },
            )
            temporary.replace(self.raw)
            self._store(self.meta, meta.model_dump_json())
            return meta
        finally:
            temporary.unlink(missing_ok=True)

    def send_sync(self, request: ModelGithubHttpRequest) -> ModelGithubHttpResponse:
        # Step selection needs the job's completed step timestamps as well as bytes.
        if self._metadata() is not None and self.job.exists():
            return ModelGithubHttpResponse.model_validate_json(self.job.read_text())
        response = self._network().send_sync(request)
        if (
            response.status == 200
            and (response.body or {}).get("status") == "completed"
        ):
            self._store(self.job, response.model_dump_json())
        return response

    @staticmethod
    def _store(path: Path, text: str) -> None:
        fd, name = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as stream:
                stream.write(text)
            Path(name).replace(path)
        finally:
            Path(name).unlink(missing_ok=True)

    def send_bytes_sync(
        self,
        request: ModelGithubHttpRequest,
        *,
        limit: int,
        keep: Literal["head", "tail"],
    ) -> ModelGithubBytesResponse:
        meta = self._log(request)
        with self.raw.open("rb") as stream:
            content, total, over = _read_capped(stream, limit=limit, keep=keep)
        return ModelGithubBytesResponse(
            status=200,
            headers=meta.headers,
            content=content,
            total_bytes=total,
            over_limit=over,
            keep=keep,
        )

    def send_bytes_window_sync(
        self, request: ModelGithubHttpRequest, *, window: JobLogWindow
    ) -> ModelGithubBytesResponse:
        meta = self._log(request)
        with self.raw.open("rb") as stream:
            total, ceiling = _read_window(stream, window)
        return ModelGithubBytesResponse(
            status=200,
            headers=meta.headers,
            content=window.kept,
            total_bytes=total,
            over_limit=ceiling,
            keep="window",
        )


class HandlerGithubRepoGatewayEffect:
    """EFFECT: run one read operation against the GitHub API, typed result out."""

    handler_type: Literal["node_handler"] = "node_handler"
    handler_category: Literal["effect"] = "effect"

    async def handle(
        self, request: ModelGithubGatewayRequest
    ) -> ModelHandlerOutput[None]:
        """Resolve the token, dispatch the read, emit the typed result event."""
        if (
            request.cache_only
            and request.operation is not EnumGithubGatewayOperation.JOB_LOG_TAIL
        ):
            raise ValueError("cache_only is supported only for job_log_tail")
        if (
            request.fill_on_miss
            and request.operation is not EnumGithubGatewayOperation.JOB_LOG_TAIL
        ):
            raise ValueError("fill_on_miss is supported only for job_log_tail")
        policy = cache_policy()
        assert policy.root is not None
        cache_only = request.cache_only
        if (
            request.fill_on_miss
            and request.operation is EnumGithubGatewayOperation.JOB_LOG_TAIL
            and policy.lane_miss_fill_max == 0
        ):
            cache_only = True
        transport = (
            None
            if cache_only
            else RealGitHubReadTransport(await resolve_github_token_async())
        )
        result: GithubGatewayResult
        if request.operation in _JOB_LOG_OPERATIONS:
            # job_log_fill and an uncached job_log_tail share one path: a hit is served from
            # the host-shared cache, and a miss downloads that one job's log (the transport
            # above is None only for a cache_only tail, which refuses the miss instead).
            miss_budget = (
                MissFillBudget(
                    policy.root,
                    max_fills=policy.lane_miss_fill_max,
                    window_seconds=policy.lane_miss_fill_window_seconds,
                )
                if request.fill_on_miss and policy.lane_miss_fill_max > 0
                else None
            )
            result = await asyncio.to_thread(
                JobLogCacheTransport(
                    policy.root,
                    transport,
                    ttl_seconds=policy.ttl_seconds,
                    miss_budget=miss_budget,
                ).handle,
                request,
            )
        else:
            assert transport is not None
            result = await asyncio.to_thread(dispatch, request, transport)

        response = ModelGithubGatewayResponse(
            correlation_id=request.correlation_id,
            result=result,
        )
        return ModelHandlerOutput.for_effect(
            input_envelope_id=uuid4(),
            correlation_id=request.correlation_id,
            handler_id=_HANDLER_ID,
            events=(response,),
        )


__all__: list[str] = [
    "HandlerGithubRepoGatewayEffect",
    "lane_job_log_flag",
]
