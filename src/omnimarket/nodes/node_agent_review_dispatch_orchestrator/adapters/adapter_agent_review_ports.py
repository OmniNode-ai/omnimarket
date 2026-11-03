# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The production ports the request entry gives the dispatch handler.

Each binding reads its environment when it is built and raises ``PortNotBoundError``,
named, when that environment is absent, so a request on a host that cannot finish the
review ends before any port is called:

* ``ScmHeadReader`` reads a pull request's head through the source-control gateway node
  (``pr_text``), over the same bus call ``onex scm`` makes.
* ``ScmReviewPoster`` posts the stored receipt through the source-control write node's
  ``pr_review_post``, pinned to the head, with the receipt id as its idempotency key.
* ``FileReceiptStore`` and ``FileTerminalLog`` keep one file per key under
  ``$ONEX_STATE_DIR/agent_review``; an insert is a hard link, which the file system
  refuses when the key already holds a file, so the first insert wins.
* ``SubprocessReceiptSigner`` signs through the omniintelligence issue node
  (``node_review_receipt_issue_compute``) in a subprocess of an omniintelligence
  checkout, with the key resolved by name from the secret store at bind time
  (``signing_key_store`` in the overlay) through the host's own store identity. The key
  is never a file, a setting or an argument on a host: it lives in this process's memory
  and goes to the child's environment, never its argv.
* ``RungVoterCaller`` sends each voter to the rung its roster entry names: the lab rung
  through ``onex delegate``, and every rung the overlay's ``harness_rungs`` map names
  through ``node_agent_review_harness_rung_effect`` with that rung's harness config. A
  harness child never receives a name the signer reads, and a prompt or vote holding the
  key is refused: the vote comes back unsigned and only this process signs.

No voter, endpoint, threshold, host or deadline is spelled here: the roster, the
timeouts and the quorum come from the node overlay; the paths from the environment.
"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import os
import sys
import tempfile
import uuid
from collections.abc import Callable, Mapping, Sequence
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, Protocol, cast

from pydantic import BaseModel, SecretStr, ValidationError

from omnimarket.delegated_test_loop.lab_run_bus import ProtocolLabRunBus
from omnimarket.delegated_test_loop.lane_bus import open_lab_run_bus
from omnimarket.handlers.scm.handler_scm_dispatch import ScmBusCaller
from omnimarket.handlers.scm.scm_inprocess import open_scm_inprocess
from omnimarket.models.model_github_gateway_io import (
    EnumGithubGatewayOperation,
    ModelGithubGatewayRequest,
)
from omnimarket.models.model_repo_write_io import (
    EnumRepoWriteOperation,
    ModelRepoWriteRequest,
)
from omnimarket.models.scm import EnumScmNode

from ..models import (
    ModelAgentFinding,
    ModelAgentHarnessRung,
    ModelAgentReceipt,
    ModelAgentReceiptCandidate,
    ModelAgentReviewConfig,
    ModelAgentReviewTerminal,
    ModelAgentSigningKeyStore,
    ModelAgentVoter,
    ModelAgentVoterReview,
)
from ..protocols import ProtocolAgentReviewLab

SECRET_ENV = "LOCAL_LLM_SHARED_SECRET"
HOST_ENV_FILE_ENV = "ONEX_HOST_ENV_FILE"
#: The host's own store identity (machine identity), read from the process environment,
#: else the host env file. These name the host's access to the store, not the key.
STORE_PROVIDER = "infisical"
STORE_ADDR_ENV = "INFISICAL_ADDR"
STORE_CLIENT_ID_ENV = "INFISICAL_CLIENT_ID"
STORE_CLIENT_SECRET_ENV = "INFISICAL_CLIENT_SECRET"
STORE_PROJECT_ENV = "INFISICAL_PROJECT_ID"
STORE_IDENTITY_ENV_NAMES = (
    STORE_ADDR_ENV,
    STORE_CLIENT_ID_ENV,
    STORE_CLIENT_SECRET_ENV,
    STORE_PROJECT_ENV,
)
STATE_DIR_ENV = "ONEX_STATE_DIR"
REGISTRY_ROOT_ENV = "OMNI_HOME"
OMNIINTELLIGENCE_ENV = "ONEX_OMNIINTELLIGENCE_CHECKOUT"
PLUGIN_ROOT_ENV = "OMNI_PLUGIN_ROOT"
#: Every environment name the signer reads; none reaches a voter's child process.
SIGNER_ENV_NAMES = (SECRET_ENV, HOST_ENV_FILE_ENV, *STORE_IDENTITY_ENV_NAMES)
RUNG_LAB = "lab"
HARNESS_RUNG_MODULE = "omnimarket.nodes.node_agent_review_harness_rung_effect"
_STATE_SUBDIR = "agent_review"

ScmBusKind = Literal["inmemory", "kafka"]


class PortNotBoundError(RuntimeError):
    """A port's environment is absent on this host; the request ends before any call."""


# -- environment ------------------------------------------------------------------


def _required(environ: Mapping[str, str], name: str, purpose: str) -> str:
    value = environ.get(name, "").strip()
    if not value:
        raise PortNotBoundError(f"{name} is not set; no {purpose} is bound")
    return value


def host_env_file(environ: Mapping[str, str]) -> Path:
    """``$ONEX_HOST_ENV_FILE``, else the host env file every lab host keeps."""
    named = environ.get(HOST_ENV_FILE_ENV, "").strip()
    return Path(named) if named else Path.home() / ".omnibase" / ".env"


def host_env_value(path: Path, name: str) -> str | None:
    """The value of ``name`` in a ``KEY=VALUE`` env file, or None. Never logged."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("export "):
            line = line.removeprefix("export ").lstrip()
        key, sep, value = line.partition("=")
        if not sep or key.strip() != name:
            continue
        value = value.strip()
        if len(value) > 1 and value[:1] == value[-1:] and value[:1] in ("'", '"'):
            value = value[1:-1]
        return value or None
    return None


class ProtocolSigningKeyStore(Protocol):
    """Reads the signing key by store reference; raises ``PortNotBoundError`` if it cannot."""

    def read(self, reference: ModelAgentSigningKeyStore) -> SecretStr: ...


def _store_identity(environ: Mapping[str, str]) -> dict[str, str]:
    """The host's store identity: the process environment, else the host env file."""
    path = host_env_file(environ)
    identity: dict[str, str] = {}
    missing: list[str] = []
    for name in STORE_IDENTITY_ENV_NAMES:
        value = environ.get(name, "").strip() or (host_env_value(path, name) or "")
        if value.strip():
            identity[name] = value.strip()
        else:
            missing.append(name)
    if missing:
        raise PortNotBoundError(
            f"{', '.join(missing)} not set in the environment or in {path}; "
            "no signer is bound (the signing key is read from the secret store)"
        )
    return identity


class InfisicalSigningKeyStore:
    """The secret store as the host's own machine identity; read-only, one name per call."""

    def __init__(self, environ: Mapping[str, str]) -> None:
        self._environ = environ

    def read(self, reference: ModelAgentSigningKeyStore) -> SecretStr:
        # Imported here: the store client is only needed on a host that signs.
        from omnibase_infra.adapters._internal.adapter_infisical import (
            AdapterInfisical,
        )
        from omnibase_infra.adapters.models.model_infisical_config import (
            ModelInfisicalAdapterConfig,
        )

        if reference.provider != STORE_PROVIDER:
            raise PortNotBoundError(
                f"the signing key store names provider {reference.provider!r}, which "
                "no binding serves; no signer is bound"
            )
        identity = _store_identity(self._environ)
        adapter = AdapterInfisical(
            ModelInfisicalAdapterConfig(
                host=identity[STORE_ADDR_ENV],
                client_id=SecretStr(identity[STORE_CLIENT_ID_ENV]),
                client_secret=SecretStr(identity[STORE_CLIENT_SECRET_ENV]),
                project_id=uuid.UUID(identity[STORE_PROJECT_ENV]),
                environment_slug=reference.environment_slug,
                secret_path=reference.secret_path,
            )
        )
        where = f"{reference.environment_slug}:{reference.secret_path}"
        try:
            adapter.initialize()
            result = adapter.get_secret(reference.secret_name)
        except Exception as exc:  # any store failure ends the request: no signer
            raise PortNotBoundError(
                f"{reference.secret_name} could not be read from the secret store at "
                f"{where} ({type(exc).__name__}); no signer is bound"
            ) from None
        finally:
            adapter.shutdown()
        if not result.value.get_secret_value().strip():
            raise PortNotBoundError(
                f"{reference.secret_name} is empty in the secret store at {where}; "
                "no signer is bound"
            )
        return result.value


def signing_key(
    reference: ModelAgentSigningKeyStore, store: ProtocolSigningKeyStore
) -> str:
    """The receipt key, read by name from the store. Never logged, never put in argv."""
    return store.read(reference).get_secret_value().strip()


def _child_env(environ: Mapping[str, str], **extra: str) -> dict[str, str]:
    env = {
        k: v
        for k, v in environ.items()
        if k != "VIRTUAL_ENV" and k not in SIGNER_ENV_NAMES
    }
    env.update(extra)
    return env


async def _run(
    argv: Sequence[str],
    *,
    timeout_seconds: float,
    cwd: Path | None = None,
    stdin: bytes | None = None,
    env: Mapping[str, str] | None = None,
) -> tuple[int, str, str]:
    """Run one child to completion; a child past its timeout is killed and raises."""
    process = await asyncio.create_subprocess_exec(
        *argv,
        cwd=str(cwd) if cwd is not None else None,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=dict(env) if env is not None else None,
    )
    try:
        async with asyncio.timeout(timeout_seconds):
            out, err = await process.communicate(stdin)
    except TimeoutError:
        process.kill()
        await process.wait()
        raise
    return (
        process.returncode if process.returncode is not None else -1,
        out.decode("utf-8", "replace"),
        err.decode("utf-8", "replace"),
    )


def _tail(text: str) -> str:
    lines = [line for line in text.strip().splitlines() if line.strip()]
    return lines[-1] if lines else ""


# -- source control -----------------------------------------------------------------


class ScmBus:
    """One source-control node call over the bus, the call ``onex scm`` makes."""

    def __init__(
        self,
        *,
        kind: ScmBusKind,
        lane: str | None,
        registry_root: Path | None,
        timeout_seconds: float,
    ) -> None:
        self._kind: ScmBusKind = kind
        self._lane = lane
        self._registry_root = registry_root
        self._timeout = timeout_seconds

    def _open(
        self, node: EnumScmNode
    ) -> AbstractAsyncContextManager[ProtocolLabRunBus]:
        # The opener's parameters, by its own signature, as ``onex scm`` passes them.
        names = tuple(inspect.signature(open_lab_run_bus).parameters)
        opener = cast(
            "Callable[..., AbstractAsyncContextManager[ProtocolLabRunBus]]",
            open_lab_run_bus,
        )
        lane = None if self._kind == "inmemory" else self._lane
        # The lane declaration is read under the registry root, as ``onex scm`` does with
        # its workspace path; the in-memory bus names no broker and reads none.
        root = None if self._kind == "inmemory" else self._registry_root
        opened = opener(**dict(zip(names, (self._kind, lane, None, root), strict=True)))
        if self._kind == "inmemory":
            return open_scm_inprocess(opened, node)
        return opened

    async def call(self, node: EnumScmNode, request: BaseModel) -> dict[str, Any]:
        correlation_id = getattr(request, "correlation_id", None)
        if not isinstance(correlation_id, uuid.UUID):
            correlation_id = uuid.uuid4()
        async with self._open(node) as bus:
            terminal = await ScmBusCaller(bus).call(
                node=node,
                request=request,
                correlation_id=correlation_id,
                timeout_s=self._timeout,
            )
        payload = terminal.payload
        if (
            terminal.failed
            or payload.get("outcome") == "failed"
            or payload.get("status") == "failed"
        ):
            raise RuntimeError(
                f"{node.value} answered failed: "
                f"{payload.get('reason') or ''} {payload.get('detail') or ''}".strip()
            )
        result = payload.get("result", payload)
        return result if isinstance(result, dict) else payload


class ProtocolScmCall(Protocol):
    """One source-control node call: the typed request in, the terminal's result out."""

    async def call(self, node: EnumScmNode, request: BaseModel) -> dict[str, Any]: ...


class ScmHeadReader:
    """The head sha a pull request has now, read live through the gateway node."""

    def __init__(self, bus: ProtocolScmCall) -> None:
        self._bus = bus

    async def read_head(self, repo: str, pr: int) -> str:
        result = await self._bus.call(
            EnumScmNode.GATEWAY,
            ModelGithubGatewayRequest(
                correlation_id=uuid.uuid4(),
                operation=EnumGithubGatewayOperation.PR_TEXT,
                repo=repo,
                pr_number=pr,
                limit=1,
            ),
        )
        head = result.get("head_sha")
        if not isinstance(head, str) or not head:
            raise RuntimeError(f"the gateway named no head for {repo}#{pr}")
        return head


def render_receipt_review(receipt: ModelAgentReceipt, fence: str) -> str:
    """The review body: one summary line and the receipt in its fenced block."""
    return (
        f"Agent review receipt for head {receipt.head_sha}: quorum verdict "
        f"{receipt.quorum_verdict}, {len(receipt.voters)} counted voter(s), "
        f"{receipt.findings_count} finding cluster(s).\n\n"
        f"```{fence}\n{receipt.model_dump_json()}\n```"
    )


class ScmReviewPoster:
    """``pr_review_post`` through the write node, and the record of what it posted.

    ``find_posted`` answers from the record this poster keeps of the reviews the write
    node completed for a receipt; the write node's own marker scan (the receipt id is the
    idempotency key) answers for a host that holds no record, so a retry from anywhere
    replays the posted review instead of writing a second one.
    """

    def __init__(
        self, bus: ProtocolScmCall, *, record_root: Path, requested_by: str
    ) -> None:
        self._bus = bus
        self._root = record_root
        self._requested_by = requested_by

    def _record(self, repo: str, pr: int, receipt_id: str) -> Path:
        digest = hashlib.sha256(
            f"{repo.lower()}#{pr}#{receipt_id}".encode()
        ).hexdigest()
        return self._root / f"{digest}.json"

    async def find_posted(
        self, repo: str, pr: int, publisher_identity: str, receipt_id: str
    ) -> bool:
        return self._record(repo, pr, receipt_id).is_file()

    async def post(
        self, repo: str, pr: int, head_sha: str, receipt: ModelAgentReceipt, fence: str
    ) -> None:
        result = await self._bus.call(
            EnumScmNode.WRITE,
            ModelRepoWriteRequest(
                correlation_id=uuid.uuid4(),
                operation=EnumRepoWriteOperation.PR_REVIEW_POST,
                repo=repo,
                idempotency_key=f"agent-review-{receipt.id}",
                requested_by_lane=self._requested_by,
                pr_number=pr,
                expected_head_sha=head_sha,
                body=render_receipt_review(receipt, fence),
            ),
        )
        review_id = result.get("review_id")
        if not isinstance(review_id, int):
            raise RuntimeError("the write node completed with no review id")
        _write_once(
            self._record(repo, pr, receipt.id),
            json.dumps(
                {
                    "repo": repo,
                    "pr": pr,
                    "head_sha": head_sha,
                    "receipt_id": receipt.id,
                    "review_id": review_id,
                    "html_url": result.get("html_url"),
                    "replayed": bool(result.get("replayed")),
                },
                sort_keys=True,
            ),
        )


# -- durable state --------------------------------------------------------------------


def _write_once(path: Path, text: str) -> bool:
    """Write ``text`` at ``path`` unless a file is there; True when this call wrote it."""
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temp = tempfile.mkstemp(dir=path.parent, prefix=".pending-")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temp, path)
        except FileExistsError:
            return False
        return True
    finally:
        os.unlink(temp)


def _keyed(root: Path, key: str) -> Path:
    return root / f"{hashlib.sha256(key.encode()).hexdigest()}.json"


class FileReceiptStore:
    """One immutable receipt file per ``repo#pr#head``; the first insert wins."""

    def __init__(self, root: Path) -> None:
        self._root = root

    async def get(self, key: str) -> ModelAgentReceipt | None:
        path = _keyed(self._root, key)
        if not path.is_file():
            return None
        document = json.loads(path.read_text(encoding="utf-8"))
        if document.get("key") != key:
            raise RuntimeError(f"{path} holds another key")
        return ModelAgentReceipt.model_validate(document["receipt"])

    async def insert_if_absent(
        self, key: str, receipt: ModelAgentReceipt
    ) -> ModelAgentReceipt:
        _write_once(
            _keyed(self._root, key),
            json.dumps({"key": key, "receipt": receipt.model_dump(mode="json")}),
        )
        stored = await self.get(key)
        if stored is None:
            raise RuntimeError(f"the receipt for {key} was not stored")
        return stored


class FileTerminalLog:
    """One terminal file per command id; the first record stands."""

    def __init__(self, root: Path) -> None:
        self._root = root

    async def get(self, command_id: str) -> ModelAgentReviewTerminal | None:
        path = _keyed(self._root, command_id)
        if not path.is_file():
            return None
        return ModelAgentReviewTerminal.model_validate_json(
            path.read_text(encoding="utf-8")
        )

    async def record(self, terminal: ModelAgentReviewTerminal) -> None:
        _write_once(_keyed(self._root, terminal.command_id), terminal.model_dump_json())


class SystemClock:
    """The time of a candidate and its nonce."""

    def now(self) -> datetime:
        return datetime.now(UTC)

    def new_id(self) -> str:
        return str(uuid.uuid4())


# -- the signer ------------------------------------------------------------------------

# Runs inside the omniintelligence checkout: the issue node's own request and handler.
SIGN_PROGRAM = """
import json, os, sys
from omniintelligence.nodes.node_review_receipt_issue_compute.handlers.handler_issue_agent_receipt import HandlerIssueAgentReceipt
from omniintelligence.nodes.node_review_receipt_issue_compute.models.model_issue_agent_receipt_request import ModelIssueAgentReceiptRequest
fields = json.load(sys.stdin)
fields["key"] = os.environ.pop("LOCAL_LLM_SHARED_SECRET").encode("utf-8")
receipt = HandlerIssueAgentReceipt().handle(ModelIssueAgentReceiptRequest.model_validate(fields))
sys.stdout.write(receipt.model_dump_json())
"""


class SubprocessReceiptSigner:
    """The omniintelligence issue node, run in a subprocess of its checkout."""

    def __init__(
        self,
        *,
        checkout: Path,
        key: str,
        environ: Mapping[str, str],
        timeout_seconds: float,
    ) -> None:
        if not (checkout / "pyproject.toml").is_file():
            raise PortNotBoundError(
                f"{OMNIINTELLIGENCE_ENV}={checkout} is not an omniintelligence checkout; "
                "no signer is bound"
            )
        self._checkout = checkout
        self._key = key
        self._environ = environ
        self._timeout = timeout_seconds

    async def sign(self, candidate: ModelAgentReceiptCandidate) -> ModelAgentReceipt:
        rc, out, err = await _run(
            ("uv", "run", "--frozen", "python", "-c", SIGN_PROGRAM),
            cwd=self._checkout,
            stdin=candidate.model_dump_json().encode("utf-8"),
            env=_child_env(self._environ, **{SECRET_ENV: self._key}),
            timeout_seconds=self._timeout,
        )
        if rc != 0:
            raise RuntimeError(f"the signer exited {rc}: {_tail(err)}")
        document = json.loads(out)
        document.pop("schema_version", None)
        return ModelAgentReceipt.model_validate(document)


# -- the voters ------------------------------------------------------------------------

REVIEW_INSTRUCTIONS = (
    "You are one voter of an adversarial code review of one pull request head. Read the "
    "diff below and report only defects the diff introduces that would break behaviour, "
    "security, data or the build. Reply with one JSON object and nothing else, of the "
    'form {"findings": [{"path": "<file path>", "line": <line in the new file>, '
    '"severity": "critical" | "error" | "warning" | "info", "claim": "<one sentence>"}]}. '
    "Use critical or error only for a defect you can point at in the diff. An empty "
    "findings list means you found no defect."
)


def review_prompt(repo: str, pr: int, head_sha: str, diff: str) -> str:
    return (
        f"{REVIEW_INSTRUCTIONS}\n\nRepository: {repo}\nPull request: {pr}\n"
        f"Head commit: {head_sha}\n\nDiff:\n{diff}\n"
    )


def parse_findings(text: str) -> tuple[ModelAgentFinding, ...]:
    """The findings of a voter's JSON answer; anything else is no vote."""
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end < start:
        raise ValueError("the answer holds no JSON object")
    document = json.loads(text[start : end + 1])
    findings = document.get("findings") if isinstance(document, dict) else None
    if not isinstance(findings, list):
        raise ValueError("the answer has no findings list")
    try:
        return tuple(ModelAgentFinding.model_validate(item) for item in findings)
    except ValidationError as exc:
        raise ValueError(
            f"a finding is malformed: {exc.error_count()} error(s)"
        ) from None


class CloneDiffReader:
    """The pull request's diff from the registry clone: merge base to the head commit."""

    def __init__(self, *, registry_root: Path, timeout_seconds: float) -> None:
        self._root = registry_root
        self._timeout = timeout_seconds

    async def diff(self, repo: str, head_sha: str) -> str:
        clone = self._root / repo.split("/", 1)[1]
        rc, base, err = await _run(
            ("git", "-C", str(clone), "merge-base", "origin/HEAD", head_sha),
            timeout_seconds=self._timeout,
        )
        if rc != 0:
            raise RuntimeError(
                f"{clone} cannot place {head_sha} against origin/HEAD: {_tail(err)}"
            )
        rc, text, err = await _run(
            ("git", "-C", str(clone), "diff", base.strip(), head_sha),
            timeout_seconds=self._timeout,
        )
        if rc != 0 or not text.strip():
            raise RuntimeError(f"no diff for {repo} at {head_sha}: {_tail(err)}")
        return text


class DelegateLabVoter:
    """The lab rung: one ``onex delegate`` code review run, with its receipt as provenance."""

    def __init__(
        self,
        *,
        onex: Path,
        state_root: Path,
        diffs: CloneDiffReader,
        environ: Mapping[str, str],
        timeout_seconds: float,
    ) -> None:
        if not onex.is_file():
            raise PortNotBoundError(f"{onex} does not exist; no lab rung is bound")
        self._onex = onex
        self._state_root = state_root
        self._diffs = diffs
        self._environ = environ
        self._timeout = timeout_seconds

    async def review(
        self, voter: ModelAgentVoter, repo: str, pr: int, head_sha: str
    ) -> ModelAgentVoterReview:
        prompt = review_prompt(
            repo, pr, head_sha, await self._diffs.diff(repo, head_sha)
        )
        with tempfile.TemporaryDirectory(prefix="agent-review-lab-") as scratch:
            prompt_file = Path(scratch) / "prompt.md"
            prompt_file.write_text(prompt, encoding="utf-8")
            rc, out, err = await _run(
                (
                    str(self._onex),
                    "delegate",
                    "--prompt-file",
                    str(prompt_file),
                    "--task-type",
                    "code_review",
                    "--bus",
                    "inmemory",
                    "--locus",
                    "in-process",
                    "--state-root",
                    str(self._state_root),
                    "--json",
                ),
                env=_child_env(self._environ),
                timeout_seconds=self._timeout,
            )
        if rc != 0:
            refusal = next(
                (line for line in err.splitlines() if line.startswith("Error:")),
                _tail(err),
            )
            raise RuntimeError(f"the lab rung exited {rc}: {refusal}")
        document = json.loads(_tail(out))
        receipt = document.get("receipt", document)
        result = receipt.get("result", receipt) if isinstance(receipt, dict) else {}
        accepted = [
            attempt
            for attempt in result.get("attempts") or []
            if attempt.get("acceptance_decision") == "accept"
        ]
        if not accepted:
            raise RuntimeError("the lab run accepted no attempt")
        attempt = accepted[-1]
        return ModelAgentVoterReview(
            identity=voter.identity,
            endpoint=str(attempt.get("host") or ""),
            model=str(attempt.get("model_id") or ""),
            delegation_event_id=str(result.get("correlation_id") or ""),
            findings=parse_findings(str(result.get("response") or "")),
        )


class HarnessRungVoter:
    """A harness rung: ``node_agent_review_harness_rung_effect`` run as its own process.

    The rung's config rides in the request; the child's environment holds no name the
    signer reads; a prompt or a vote that holds the signing key is refused, so the key
    cannot reach a harness session or come back inside a review.
    """

    def __init__(
        self,
        *,
        rung: ModelAgentHarnessRung,
        diffs: CloneDiffReader,
        environ: Mapping[str, str],
        withheld: Sequence[str],
        timeout_seconds: float,
    ) -> None:
        _required(environ, PLUGIN_ROOT_ENV, "harness rung (harness-delegate)")
        self._rung = rung
        self._diffs = diffs
        self._environ = environ
        self._withheld = tuple(value for value in withheld if value)
        self._timeout = timeout_seconds

    def _holds_withheld(self, text: str) -> bool:
        return any(value in text for value in self._withheld)

    async def review(
        self, voter: ModelAgentVoter, repo: str, pr: int, head_sha: str
    ) -> ModelAgentVoterReview:
        prompt = review_prompt(
            repo, pr, head_sha, await self._diffs.diff(repo, head_sha)
        )
        if self._holds_withheld(prompt):
            raise RuntimeError("the review prompt holds the signing key; no call made")
        request = {
            "identity": voter.identity,
            "model": voter.model,
            "rung": self._rung.model_dump(mode="json"),
            "prompt": prompt,
        }
        rc, out, err = await _run(
            (sys.executable, "-m", HARNESS_RUNG_MODULE),
            stdin=json.dumps(request).encode("utf-8"),
            env=_child_env(self._environ),
            timeout_seconds=self._timeout,
        )
        if rc != 0:
            raise RuntimeError(f"the {voter.rung} rung exited {rc}: {_tail(err)}")
        if self._holds_withheld(out):
            raise RuntimeError(f"the {voter.rung} rung's vote holds the signing key")
        return ModelAgentVoterReview.model_validate_json(out)


class RungVoterCaller:
    """Send each voter to the rung its roster entry names; an unbound rung casts no vote."""

    def __init__(self, rungs: Mapping[str, ProtocolAgentReviewLab]) -> None:
        self._rungs = dict(rungs)

    async def review(
        self, voter: ModelAgentVoter, repo: str, pr: int, head_sha: str
    ) -> ModelAgentVoterReview:
        rung = self._rungs.get(voter.rung)
        if rung is None:
            raise PortNotBoundError(f"no binding serves rung {voter.rung!r}")
        return await rung.review(voter, repo, pr, head_sha)


# -- assembly ----------------------------------------------------------------------------


@dataclass(frozen=True)
class ModelAgentReviewPorts:
    """Every port the handler takes, bound for production."""

    head: ScmHeadReader
    lab: RungVoterCaller
    signer: SubprocessReceiptSigner
    store: FileReceiptStore
    poster: ScmReviewPoster
    terminal_log: FileTerminalLog
    clock: SystemClock


def build_production_ports(
    config: ModelAgentReviewConfig,
    *,
    scm_bus: ScmBusKind,
    scm_lane: str | None,
    environ: Mapping[str, str] | None = None,
    key_store: ProtocolSigningKeyStore | None = None,
) -> ModelAgentReviewPorts:
    """Bind every port from the environment, or raise ``PortNotBoundError`` naming one.

    The signing key is read by the overlay's ``signing_key_store`` reference; ``key_store``
    replaces the host's store identity only in a test.
    """
    env: Mapping[str, str] = os.environ if environ is None else environ
    state = Path(_required(env, STATE_DIR_ENV, "receipt store")) / _STATE_SUBDIR
    registry = Path(_required(env, REGISTRY_ROOT_ENV, "diff reader"))
    checkout = Path(_required(env, OMNIINTELLIGENCE_ENV, "signer"))
    key = signing_key(
        config.signing_key_store,
        key_store if key_store is not None else InfisicalSigningKeyStore(env),
    )
    if scm_bus == "kafka" and not scm_lane:
        raise PortNotBoundError("a kafka source-control bus needs a lane")
    bus = ScmBus(
        kind=scm_bus,
        lane=scm_lane,
        registry_root=registry,
        timeout_seconds=config.lab_call_timeout_seconds,
    )
    diffs = CloneDiffReader(
        registry_root=registry, timeout_seconds=config.lab_call_timeout_seconds
    )
    rungs: dict[str, ProtocolAgentReviewLab] = {}
    for rung in sorted({voter.rung for voter in config.roster}):
        if rung == RUNG_LAB:
            rungs[rung] = DelegateLabVoter(
                onex=registry / "omnibase_infra" / "scripts" / "onex",
                state_root=registry / ".onex_state",
                diffs=diffs,
                environ=env,
                timeout_seconds=config.lab_call_timeout_seconds,
            )
        elif rung in config.harness_rungs:
            rungs[rung] = HarnessRungVoter(
                rung=config.harness_rungs[rung],
                diffs=diffs,
                environ=env,
                withheld=(key,),
                timeout_seconds=config.lab_call_timeout_seconds,
            )
        else:
            raise PortNotBoundError(
                f"the roster names rung {rung!r}, which no binding serves"
            )
    return ModelAgentReviewPorts(
        head=ScmHeadReader(bus),
        lab=RungVoterCaller(rungs),
        signer=SubprocessReceiptSigner(
            checkout=checkout,
            key=key,
            environ=env,
            timeout_seconds=config.lab_call_timeout_seconds,
        ),
        store=FileReceiptStore(state / "receipts"),
        poster=ScmReviewPoster(
            bus, record_root=state / "posted", requested_by=config.reviewer_id
        ),
        terminal_log=FileTerminalLog(state / "terminals"),
        clock=SystemClock(),
    )


__all__ = [
    "CloneDiffReader",
    "DelegateLabVoter",
    "FileReceiptStore",
    "FileTerminalLog",
    "HarnessRungVoter",
    "InfisicalSigningKeyStore",
    "ModelAgentReviewPorts",
    "PortNotBoundError",
    "ProtocolScmCall",
    "ProtocolSigningKeyStore",
    "RungVoterCaller",
    "ScmBus",
    "ScmHeadReader",
    "ScmReviewPoster",
    "SubprocessReceiptSigner",
    "SystemClock",
    "build_production_ports",
    "host_env_value",
    "parse_findings",
    "render_receipt_review",
    "review_prompt",
    "signing_key",
]
