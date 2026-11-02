# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Handler of node_lab_receipt_check_effect (OMN-21826, task L2 of the lab-receipt design note).

Operator ruling 2026-10-31T17:16:31Z (ticket OMN-21823): every pull request that changes
something that runs carries a lab receipt that its branch revision was installed on the lab and
observed running at the PR head. The rule is a validator in omnibase_core (``lab_receipt``); this
node is the one impure consumer that gathers its facts and makes the single call to it.

One request names a pull request, the head sha the caller will merge and a phase. The handler

1. reads the pull request and refuses (``UNREADABLE``) a head that moved past the request;
2. reads every changed file (every page; a list that may be cut short is unreadable, never a
   shorter list that passes), so that a file nobody classified still reaches the validator;
3. takes the cited tickets from the title and reads the criteria statements of each
   ``contracts/<ticket>.yaml`` at that head (a ticket with no contract at the head adds none);
4. reads every receipt record of the pull request from the carrier, whatever head each names, so
   that the validator can name an older head's receipt as a head mismatch;
5. in phase ``verify`` only, reads the lab for each host a receipt names. A lab that cannot be read
   is passed on as no lab read, which the validator refuses (``lab_read_unavailable``); in phase
   ``present`` the lab reader is never called and the repository token is the only credential;
6. makes one call to ``check_lab_receipt`` with the contract of the core validator.

No rule text, path pattern, host name or threshold is a literal here: the parameters of the
gathering are in ``contract.yaml`` (``lab_receipt_check``) and every rule is a row of the core
contract. An error issue blocks the merge, and so does anything unreadable.
"""

from __future__ import annotations

import base64
import binascii
import logging
import re
from collections.abc import Callable
from pathlib import Path
from typing import Final
from urllib.parse import quote

import yaml
from omnibase_core.enums.enum_lab_receipt_phase import EnumLabReceiptPhase
from omnibase_core.models.contracts.subcontracts.model_validator_subcontract import (
    ModelValidatorSubcontract,
)
from omnibase_core.models.lab_receipt.model_lab_receipt_changed_file import (
    ModelLabReceiptChangedFile,
)
from omnibase_core.models.lab_receipt.model_lab_receipt_check_input import (
    ModelLabReceiptCheckInput,
)
from omnibase_core.models.lab_receipt.model_lab_receipt_criterion import (
    ModelLabReceiptCriterion,
)
from omnibase_core.models.lab_receipt.model_lab_receipt_document import (
    ModelLabReceiptDocument,
)
from omnibase_core.models.lab_receipt.model_lab_receipt_facts import (
    ModelLabReceiptFacts,
)
from omnibase_core.models.lab_receipt.model_lab_receipt_lab_read import (
    ModelLabReceiptLabRead,
)
from omnibase_core.models.lab_receipt.model_lab_receipt_record import (
    ModelLabReceiptRecord,
)
from omnibase_core.validation.checker_lab_receipt import check_lab_receipt
from omnibase_core.validation.validator_lab_receipt import ValidatorLabReceipt

from omnimarket.github_landing.github_landing_requests import (
    pull_request_request,
    split_repository,
)
from omnimarket.github_landing.github_landing_transport import (
    GithubLandingTransportError,
    UrllibGithubLandingTransport,
)
from omnimarket.github_landing.model_github_http_exchange import (
    ModelGithubHttpRequest,
    ModelGithubHttpResponse,
)
from omnimarket.inference.secret_store_resolver import resolve_api_key_async
from omnimarket.nodes.contract_topics import (
    contract_publish_topics,
    contract_secret_ref,
    contract_section,
)
from omnimarket.nodes.node_lab_receipt_check_effect.models import (
    EnumLabReceiptVerdict,
    ModelLabReceiptCheckRequest,
    ModelLabReceiptCheckResult,
    ModelLabReceiptIssueView,
)
from omnimarket.nodes.node_lab_receipt_check_effect.protocols import (
    ProtocolLabInstallReader,
    ProtocolLabReceiptCarrier,
    ProtocolLabReceiptEventPublisher,
    ProtocolLabReceiptGithubTransport,
)

_log = logging.getLogger(__name__)
_CONTRACT_PATH: Final = Path(__file__).resolve().parents[1] / "contract.yaml"
_SECTION: Final = "lab_receipt_check"
_GITHUB_SECRET: Final = "GITHUB_TOKEN"
_NOT_FOUND: Final = 404
_OK: Final = 200

ContractLoader = Callable[[], ModelValidatorSubcontract]


class _UnreadableError(Exception):
    """Internal: a read the check cannot continue without failed; the message is the reason."""


class _Settings:
    """The gathering parameters, read once from the node contract; a missing key fails closed."""

    def __init__(self, contract_path: Path) -> None:
        def text(key: str) -> str:
            value = contract_section(contract_path, _SECTION, key)
            if not isinstance(value, str) or not value:
                raise ValueError(f"{contract_path}: {_SECTION}.{key} must be a string")
            return value

        def number(key: str) -> int:
            value = contract_section(contract_path, _SECTION, key)
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(
                    f"{contract_path}: {_SECTION}.{key} must be a positive integer"
                )
            return value

        letters = contract_section(contract_path, _SECTION, "file_status_letters")
        if not isinstance(letters, dict) or not all(
            isinstance(k, str) and isinstance(v, str) and v for k, v in letters.items()
        ):
            raise ValueError(
                f"{contract_path}: {_SECTION}.file_status_letters must map names to letters"
            )
        self.ticket = re.compile(text("ticket_pattern"))
        self.contract_path_template = text("ticket_contract_path")
        self.document_name_template = text("document_name")
        self.files_per_page = number("files_per_page")
        self.max_file_pages = number("max_file_pages")
        self.status_letters: dict[str, str] = dict(letters)
        self.renamed_from_letter = text("renamed_from_letter")


class HandlerLabReceiptCheck:
    """EFFECT: gather the facts of one pull request head and call the core lab_receipt validator."""

    def __init__(
        self,
        transport: ProtocolLabReceiptGithubTransport | None = None,
        carrier: ProtocolLabReceiptCarrier | None = None,
        lab_reader: ProtocolLabInstallReader | None = None,
        contract_loader: ContractLoader | None = None,
        event_bus: ProtocolLabReceiptEventPublisher | None = None,
    ) -> None:
        self._transport = transport
        self._carrier = carrier
        self._lab_reader = lab_reader
        self._contract_loader = contract_loader or _core_contract
        self._event_bus = event_bus
        self._settings = _Settings(_CONTRACT_PATH)
        self._completed_topic = contract_publish_topics(_CONTRACT_PATH)[0]

    async def handle(
        self, request: ModelLabReceiptCheckRequest
    ) -> ModelLabReceiptCheckResult:
        """Check one pull request head; a failure of any read blocks, never passes."""
        try:
            result = await self._check(request)
        except _UnreadableError as error:
            result = _unreadable(request, str(error))
        except Exception as error:
            _log.error(
                "lab receipt check %s#%d failed", request.repo, request.pr_number
            )
            result = _unreadable(
                request, f"the check could not run ({type(error).__name__})"
            )
        await self._publish(result)
        return result

    async def _publish(self, result: ModelLabReceiptCheckResult) -> None:
        """The completed event; a publish failure is logged and never changes the verdict."""
        if self._event_bus is None:
            return
        try:
            await self._event_bus.publish(
                self._completed_topic,
                key=f"{result.repo}#{result.pr_number}".encode(),
                value=result.model_dump_json().encode(),
            )
        except Exception:
            _log.exception(
                "lab receipt check: failed to publish on %s", self._completed_topic
            )

    async def _check(
        self, request: ModelLabReceiptCheckRequest
    ) -> ModelLabReceiptCheckResult:
        transport = self._transport or await _live_transport()
        title = await self._read_title(transport, request)
        changed = await self._read_files(transport, request)
        criteria = await self._read_criteria(transport, request, title)
        receipts = await self._read_receipts(request)
        lab_reads, lab_note = await self._read_lab(request, receipts)
        facts = ModelLabReceiptFacts(
            repository=request.repo,
            head_sha=request.head_sha,
            changed_files=changed,
            criteria=criteria,
            receipts=receipts,
            lab_reads=lab_reads,
        )
        name = self._settings.document_name_template.format(
            repo=request.repo, pr_number=request.pr_number
        )
        verdict = check_lab_receipt(
            ModelLabReceiptCheckInput(
                source_name=name,
                document=ModelLabReceiptDocument(phase=request.phase, facts=facts),
                contract=self._contract_loader(),
            )
        )
        issues = tuple(
            ModelLabReceiptIssueView(
                code=issue.code or "",
                severity=str(issue.severity),
                message=issue.message,
            )
            for issue in verdict.issues
        )
        blocked = verdict.exit_code != 0
        if blocked:
            outcome = EnumLabReceiptVerdict.REFUSED
        elif verdict.required:
            outcome = EnumLabReceiptVerdict.PASS
        else:
            outcome = EnumLabReceiptVerdict.NOT_REQUIRED
        reason = "; ".join(i.message for i in issues if i.code) if blocked else ""
        if blocked and lab_note:
            reason = f"{reason}; {lab_note}"
        return ModelLabReceiptCheckResult(
            correlation_id=request.correlation_id,
            repo=request.repo,
            pr_number=request.pr_number,
            head_sha=request.head_sha,
            phase=request.phase,
            verdict=outcome,
            merge_blocked=blocked,
            required=verdict.required,
            classes=verdict.classes,
            issues=issues,
            reason=reason,
        )

    async def _read_title(
        self,
        transport: ProtocolLabReceiptGithubTransport,
        request: ModelLabReceiptCheckRequest,
    ) -> str:
        response = await _send(
            transport, pull_request_request(request.repo, request.pr_number)
        )
        body = response.body
        if response.status != _OK or body is None:
            raise _UnreadableError(
                f"the pull request could not be read (HTTP {response.status})"
            )
        head = body.get("head")
        head_sha = head.get("sha") if isinstance(head, dict) else None
        if head_sha != request.head_sha:
            raise _UnreadableError(
                "the pull request head is not the one the caller will merge "
                f"({str(head_sha)[:12]} against {request.head_sha[:12]})"
            )
        if body.get("state") != "open" or body.get("merged") is True:
            raise _UnreadableError("the pull request is not open")
        title = body.get("title")
        if not isinstance(title, str):
            raise _UnreadableError("the pull request carries no title")
        return title

    async def _read_files(
        self,
        transport: ProtocolLabReceiptGithubTransport,
        request: ModelLabReceiptCheckRequest,
    ) -> tuple[ModelLabReceiptChangedFile, ...]:
        """Every changed file, every page; a list that may have been cut short is unreadable."""
        settings = self._settings
        owner, name = split_repository(request.repo)
        files: list[ModelLabReceiptChangedFile] = []
        for page in range(1, settings.max_file_pages + 1):
            response = await _send(
                transport,
                ModelGithubHttpRequest(
                    method="GET",
                    path=(
                        f"/repos/{owner}/{name}/pulls/{request.pr_number}/files"
                        f"?per_page={settings.files_per_page}&page={page}"
                    ),
                ),
            )
            entries = _list_body(response)
            for entry in entries:
                files.extend(self._changed(entry))
            if len(entries) < settings.files_per_page:
                return tuple(files)
        raise _UnreadableError(
            "the changed file list may be longer than "
            f"{settings.max_file_pages * settings.files_per_page} files and was not read in full"
        )

    def _changed(self, entry: object) -> list[ModelLabReceiptChangedFile]:
        settings = self._settings
        if not isinstance(entry, dict):
            raise _UnreadableError("a changed file entry is not an object")
        path = entry.get("filename")
        status = entry.get("status")
        if not isinstance(path, str) or not path or not isinstance(status, str):
            raise _UnreadableError("a changed file entry carries no path or status")
        letter = settings.status_letters.get(status)
        if letter is None:
            raise _UnreadableError(f"a changed file has the unknown status {status!r}")
        found = [ModelLabReceiptChangedFile(path=path, status=letter)]
        previous = entry.get("previous_filename")
        if isinstance(previous, str) and previous and previous != path:
            found.append(
                ModelLabReceiptChangedFile(
                    path=previous, status=settings.renamed_from_letter
                )
            )
        return found

    async def _read_criteria(
        self,
        transport: ProtocolLabReceiptGithubTransport,
        request: ModelLabReceiptCheckRequest,
        title: str,
    ) -> tuple[ModelLabReceiptCriterion, ...]:
        criteria: list[ModelLabReceiptCriterion] = []
        for ticket in sorted(set(self._settings.ticket.findall(title))):
            text = await self._read_contract(transport, request, ticket)
            if text is not None:
                criteria.extend(_statements(ticket, text))
        return tuple(criteria)

    async def _read_contract(
        self,
        transport: ProtocolLabReceiptGithubTransport,
        request: ModelLabReceiptCheckRequest,
        ticket: str,
    ) -> str | None:
        path = self._settings.contract_path_template.format(ticket=ticket)
        owner, name = split_repository(request.repo)
        response = await _send(
            transport,
            ModelGithubHttpRequest(
                method="GET",
                path=f"/repos/{owner}/{name}/contents/{quote(path)}?ref={request.head_sha}",
            ),
        )
        if response.status == _NOT_FOUND:
            return None
        body = response.body
        if response.status != _OK or body is None:
            raise _UnreadableError(f"{path} could not be read (HTTP {response.status})")
        content = body.get("content")
        if body.get("encoding") != "base64" or not isinstance(content, str):
            raise _UnreadableError(f"{path} came back in a form that cannot be read")
        try:
            return base64.b64decode(content).decode("utf-8")
        except (binascii.Error, UnicodeDecodeError) as error:
            raise _UnreadableError(f"{path} is not readable text") from error

    async def _read_receipts(
        self, request: ModelLabReceiptCheckRequest
    ) -> tuple[ModelLabReceiptRecord, ...]:
        if self._carrier is None:
            raise _UnreadableError("no receipt carrier is configured")
        try:
            return tuple(
                await self._carrier.read_receipts(request.repo, request.pr_number)
            )
        except Exception as error:
            raise _UnreadableError(
                f"the receipt carrier could not be read ({type(error).__name__})"
            ) from error

    async def _read_lab(
        self,
        request: ModelLabReceiptCheckRequest,
        receipts: tuple[ModelLabReceiptRecord, ...],
    ) -> tuple[tuple[ModelLabReceiptLabRead, ...] | None, str]:
        """The lab reads for the verify phase, or None (and why) when the lab cannot be read.

        The present phase never calls the lab reader. None is not a default: the validator refuses a
        verify run supplied no lab read, so an unreachable lab blocks the merge.
        """
        if request.phase is not EnumLabReceiptPhase.VERIFY:
            return None, ""
        if self._lab_reader is None:
            return None, "no lab reader is configured"
        hosts = tuple(sorted({r.host for r in receipts if r.host}))
        try:
            return tuple(await self._lab_reader.read_installs(hosts)), ""
        except Exception as error:
            return None, f"the lab could not be read ({type(error).__name__})"


def _unreadable(
    request: ModelLabReceiptCheckRequest, failure: str
) -> ModelLabReceiptCheckResult:
    return ModelLabReceiptCheckResult(
        correlation_id=request.correlation_id,
        repo=request.repo,
        pr_number=request.pr_number,
        head_sha=request.head_sha,
        phase=request.phase,
        verdict=EnumLabReceiptVerdict.UNREADABLE,
        merge_blocked=True,
        reason=f"LAB_RECEIPT_UNREADABLE: {failure}",
    )


def _statements(ticket: str, text: str) -> list[ModelLabReceiptCriterion]:
    """The criterion statements a ticket contract records (requirements -> acceptance)."""
    try:
        raw = yaml.safe_load(text)
    except yaml.YAMLError as error:
        raise _UnreadableError(f"the contract of {ticket} is not valid YAML") from error
    if not isinstance(raw, dict):
        raise _UnreadableError(f"the contract of {ticket} is not a mapping")
    found: list[ModelLabReceiptCriterion] = []
    requirements = raw.get("requirements")
    for requirement in requirements if isinstance(requirements, list) else []:
        acceptance = (
            requirement.get("acceptance") if isinstance(requirement, dict) else None
        )
        for item in acceptance if isinstance(acceptance, list) else []:
            statement = item.get("statement") if isinstance(item, dict) else None
            if isinstance(statement, str) and statement:
                found.append(
                    ModelLabReceiptCriterion(ticket=ticket, statement=statement)
                )
    return found


def _list_body(response: ModelGithubHttpResponse) -> list[object]:
    body = response.body
    if response.status != _OK or body is None:
        raise _UnreadableError(
            f"the changed files could not be read (HTTP {response.status})"
        )
    value = body.get("value")
    if not isinstance(value, list):
        raise _UnreadableError("the changed file list came back in an unreadable form")
    return value


async def _send(
    transport: ProtocolLabReceiptGithubTransport, request: ModelGithubHttpRequest
) -> ModelGithubHttpResponse:
    try:
        return await transport.send(request)
    except GithubLandingTransportError as error:
        raise _UnreadableError(
            f"GitHub did not answer ({type(error).__name__})"
        ) from error


async def _live_transport() -> UrllibGithubLandingTransport:
    ref = contract_secret_ref(_CONTRACT_PATH, _GITHUB_SECRET)
    secret = await resolve_api_key_async(ref, env_var_fallback=ref)
    if secret is None:
        raise _UnreadableError(
            f"api_key_ref {ref!r} resolved to None; set it in the secret store"
        )
    return UrllibGithubLandingTransport(secret)


def _core_contract() -> ModelValidatorSubcontract:
    """The committed contract of the core validator; the node holds no copy of the rows."""
    return ValidatorLabReceipt().contract


__all__ = ["HandlerLabReceiptCheck"]
