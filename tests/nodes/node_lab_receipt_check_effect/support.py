# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Recorded collaborators for node_lab_receipt_check_effect (OMN-21826).

Every collaborator is built from a core ``lab_receipt`` facts document, so the planted fixtures of
omnibase_core drive the node through the same seams a live run uses: a recorded GitHub (pull
request, paginated files, ticket contracts), a receipt carrier and a lab install reader.
"""

from __future__ import annotations

import base64
import json
import re
from pathlib import Path
from uuid import uuid4

import yaml
from omnibase_core.enums.enum_lab_receipt_phase import EnumLabReceiptPhase
from omnibase_core.models.lab_receipt.model_lab_receipt_document import (
    ModelLabReceiptDocument,
)
from omnibase_core.models.lab_receipt.model_lab_receipt_lab_read import (
    ModelLabReceiptLabRead,
)
from omnibase_core.models.lab_receipt.model_lab_receipt_record import (
    ModelLabReceiptRecord,
)

from omnimarket.github_landing.model_github_http_exchange import (
    ModelGithubHttpRequest,
    ModelGithubHttpResponse,
)
from omnimarket.nodes.node_lab_receipt_check_effect.models import (
    ModelLabReceiptCheckRequest,
)

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "lab_receipt_core"

# Independent of the node contract on purpose: a wrong map in the contract must fail a test.
GITHUB_STATUS = {
    "A": "added",
    "M": "modified",
    "D": "removed",
    "R": "renamed",
    "C": "copied",
    "T": "changed",
    "U": "unchanged",
}

_PAGE = re.compile(r"per_page=(\d+)&page=(\d+)")


def load_document(name: str) -> ModelLabReceiptDocument:
    return ModelLabReceiptDocument.model_validate_json(
        (FIXTURES / f"{name}.json").read_text("utf-8")
    )


def fixture_names() -> list[str]:
    return sorted(
        path.stem
        for path in FIXTURES.glob("*.json")
        if not path.name.startswith("golden")
    )


def make_request(
    document: ModelLabReceiptDocument,
    phase: EnumLabReceiptPhase | None = None,
    pr_number: int = 7,
) -> ModelLabReceiptCheckRequest:
    return ModelLabReceiptCheckRequest(
        correlation_id=uuid4(),
        repo=document.facts.repository,
        pr_number=pr_number,
        head_sha=document.facts.head_sha,
        phase=phase or document.phase,
    )


class RecordedGithub:
    """A recorded pull request, its changed files (paged as GitHub pages them) and contracts."""

    def __init__(
        self,
        document: ModelLabReceiptDocument,
        *,
        pr_head: str | None = None,
        title: str | None = None,
        files_status: int = 200,
        contract_status: int = 200,
        extra_files: int = 0,
    ) -> None:
        facts = document.facts
        tickets = sorted({c.ticket for c in facts.criteria})
        self.title = title if title is not None else f"feat({','.join(tickets)}): x"
        self.pr_head = pr_head or facts.head_sha
        self.files_status = files_status
        self.contract_status = contract_status
        self.contracts: dict[str, str] = {}
        for ticket in tickets:
            statements = [c.statement for c in facts.criteria if c.ticket == ticket]
            self.contracts[ticket] = yaml.safe_dump(
                {
                    "ticket_id": ticket,
                    "requirements": [
                        {
                            "id": "req-1",
                            "acceptance": [
                                {"id": f"AC{i}", "statement": s}
                                for i, s in enumerate(statements, 1)
                            ],
                        }
                    ],
                }
            )
        self.entries: list[dict[str, object]] = [
            {"filename": f.path, "status": GITHUB_STATUS[f.status]}
            for f in facts.changed_files
        ]
        self.entries += [
            {"filename": f"padding/file_{i}.md", "status": "modified"}
            for i in range(extra_files)
        ]
        self.requests: list[str] = []

    async def send(self, request: ModelGithubHttpRequest) -> ModelGithubHttpResponse:
        self.requests.append(request.path)
        path = request.path
        if "/files" in path:
            if self.files_status != 200:
                return ModelGithubHttpResponse(status=self.files_status, headers={})
            match = _PAGE.search(path)
            assert match, path
            per_page, page = int(match.group(1)), int(match.group(2))
            chunk = self.entries[(page - 1) * per_page : page * per_page]
            return ModelGithubHttpResponse(
                status=200, headers={}, body={"value": chunk}
            )
        if "/contents/" in path:
            if self.contract_status != 200:
                return ModelGithubHttpResponse(status=self.contract_status, headers={})
            for ticket, text in self.contracts.items():
                if f"/{ticket}.yaml" in path:
                    return ModelGithubHttpResponse(
                        status=200,
                        headers={},
                        body={
                            "encoding": "base64",
                            "content": base64.b64encode(text.encode()).decode(),
                        },
                    )
            return ModelGithubHttpResponse(status=404, headers={})
        assert "/pulls/" in path, path
        return ModelGithubHttpResponse(
            status=200,
            headers={},
            body={
                "title": self.title,
                "state": "open",
                "merged": False,
                "head": {"sha": self.pr_head},
            },
        )


class InMemoryCarrier:
    """Receipt records held in memory, in the order the carrier found them."""

    def __init__(
        self, receipts: tuple[ModelLabReceiptRecord, ...], *, fail: bool = False
    ) -> None:
        self._receipts = receipts
        self._fail = fail
        self.reads = 0

    async def read_receipts(
        self, repo: str, pr_number: int
    ) -> tuple[ModelLabReceiptRecord, ...]:
        self.reads += 1
        if self._fail:
            raise ConnectionError("the carrier is down")
        return self._receipts


class InMemoryLab:
    """Lab reads held in memory; ``None`` is a lab that cannot be reached."""

    def __init__(self, reads: tuple[ModelLabReceiptLabRead, ...] | None) -> None:
        self._reads = reads
        self.asked: list[tuple[str, ...]] = []

    async def read_installs(
        self, hosts: tuple[str, ...]
    ) -> tuple[ModelLabReceiptLabRead, ...]:
        self.asked.append(hosts)
        if self._reads is None:
            raise ConnectionError("the lab cannot be reached")
        return self._reads


class RecordedLab:
    """Lab reads replayed from a recorded session file: {host: {installed_revision, running}}."""

    def __init__(self, session: Path) -> None:
        self._hosts: dict[str, dict[str, object]] = json.loads(
            session.read_text("utf-8")
        )["hosts"]

    async def read_installs(
        self, hosts: tuple[str, ...]
    ) -> tuple[ModelLabReceiptLabRead, ...]:
        found: list[ModelLabReceiptLabRead] = []
        for host in hosts:
            if host not in self._hosts:
                raise ConnectionError(f"no recorded read for {host}")
            record = self._hosts[host]
            found.append(
                ModelLabReceiptLabRead(
                    host=host,
                    installed_revision=str(record["installed_revision"]),
                    running=bool(record["running"]),
                )
            )
        return tuple(found)


class RecordedCarrier:
    """Receipt records replayed from a recorded file: {"receipts": [record, ...]}."""

    def __init__(self, session: Path) -> None:
        raw = json.loads(session.read_text("utf-8"))["receipts"]
        self._receipts = tuple(ModelLabReceiptRecord.model_validate(r) for r in raw)

    async def read_receipts(
        self, repo: str, pr_number: int
    ) -> tuple[ModelLabReceiptRecord, ...]:
        return self._receipts
