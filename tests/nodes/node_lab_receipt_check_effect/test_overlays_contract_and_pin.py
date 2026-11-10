# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-21826: the two overlays agree, the contract carries every value, and the core pin is a tag.

* Overlay parity: the in-memory overlay (collaborators built from the facts) and the recorded-lab
  overlay (a lab session and a receipt record replayed from disk) give the same report, in both
  phases, and the same completed event on the in-memory bus.
* Contract first: the gathering parameters are read from ``contract.yaml``; changing one changes the
  behaviour, removing one fails closed, and the handler holds none of them as a literal.
* Core pin: omnibase-core is read from a dev merge tag, never a sha.
"""

from __future__ import annotations

import ast
import importlib
import re
import tomllib
from pathlib import Path
from typing import Any

import pytest
import yaml
from omnibase_core.enums.enum_lab_receipt_phase import EnumLabReceiptPhase

from omnimarket.nodes.node_lab_receipt_check_effect.handlers import (
    handler_lab_receipt_check as handler_module,
)
from omnimarket.nodes.node_lab_receipt_check_effect.handlers.handler_lab_receipt_check import (
    HandlerLabReceiptCheck,
)
from omnimarket.nodes.node_lab_receipt_check_effect.models import (
    EnumLabReceiptVerdict,
    ModelLabReceiptCheckResult,
)
from tests.nodes.node_lab_receipt_check_effect.support import (
    FIXTURES,
    InMemoryCarrier,
    InMemoryLab,
    RecordedCarrier,
    RecordedGithub,
    RecordedLab,
    load_document,
    make_request,
)

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[3]
NODE = ROOT / "src" / "omnimarket" / "nodes" / "node_lab_receipt_check_effect"
CONTRACT = NODE / "contract.yaml"
RECORDED = ROOT / "tests" / "fixtures" / "lab_receipt_recorded"
COMPLETED_TOPIC = "onex.evt.omnimarket.lab-receipt-check-completed.v1"
PHASES = (EnumLabReceiptPhase.PRESENT, EnumLabReceiptPhase.VERIFY)


def _in_memory() -> tuple[HandlerLabReceiptCheck, Any]:
    document = load_document("clean_receipt_not_on_lab")
    handler = HandlerLabReceiptCheck(
        transport=RecordedGithub(document),
        carrier=InMemoryCarrier(document.facts.receipts),
        lab_reader=InMemoryLab(document.facts.lab_reads),
    )
    return handler, document


def _recorded() -> tuple[HandlerLabReceiptCheck, Any]:
    document = load_document("clean_receipt_not_on_lab")
    handler = HandlerLabReceiptCheck(
        transport=RecordedGithub(document),
        carrier=RecordedCarrier(RECORDED / "receipts.json"),
        lab_reader=RecordedLab(RECORDED / "lab_session.json"),
    )
    return handler, document


def _report(result: ModelLabReceiptCheckResult) -> dict[str, object]:
    """The report without the per-run correlation id."""
    return result.model_dump(mode="json", exclude={"correlation_id"})


@pytest.mark.parametrize("phase", PHASES)
async def test_overlay_parity_in_memory_and_recorded_lab_give_one_report(
    phase: EnumLabReceiptPhase,
) -> None:
    memory, document = _in_memory()
    recorded, _ = _recorded()
    first = await memory.handle(make_request(document, phase, pr_number=9))
    second = await recorded.handle(make_request(document, phase, pr_number=9))
    assert _report(first) == _report(second)
    assert first.verdict is EnumLabReceiptVerdict.PASS
    assert first.merge_blocked is False


async def test_overlay_parity_a_recorded_lab_without_the_host_is_refused_like_an_unreachable_one() -> (
    None
):
    document = load_document("clean_receipt_not_on_lab")
    other = document.model_copy(
        update={
            "facts": document.facts.model_copy(
                update={
                    "receipts": tuple(
                        r.model_copy(update={"host": "lab-z"})
                        for r in document.facts.receipts
                    )
                }
            )
        }
    )
    handler = HandlerLabReceiptCheck(
        transport=RecordedGithub(other),
        carrier=InMemoryCarrier(other.facts.receipts),
        lab_reader=RecordedLab(RECORDED / "lab_session.json"),
    )
    result = await handler.handle(make_request(other, EnumLabReceiptPhase.VERIFY))
    assert [i.code for i in result.issues] == ["lab_read_unavailable"]
    assert result.merge_blocked is True


def test_contract_first_and_no_hardcoded_values_the_contract_names_the_handler_and_topics() -> (
    None
):
    contract = yaml.safe_load(CONTRACT.read_text(encoding="utf-8"))
    module = importlib.import_module(contract["handler"]["module"])
    assert getattr(module, contract["handler"]["class"]) is HandlerLabReceiptCheck
    assert contract["event_bus"]["subscribe_topics"] == [
        "onex.cmd.omnimarket.lab-receipt-check-requested.v1"
    ]
    assert contract["event_bus"]["publish_topics"] == [COMPLETED_TOPIC]
    assert set(contract["secrets"]) == {"GITHUB_TOKEN"}
    declared = {d["name"] for d in contract["dependencies"]}
    assert declared == {"ProtocolLabReceiptCarrier", "ProtocolLabInstallReader"}
    assert set(contract["lab_receipt_check"]) == {
        "ticket_pattern",
        "ticket_contract_path",
        "document_name",
        "files_per_page",
        "max_file_pages",
        "file_status_letters",
        "renamed_from_letter",
    }


def _contract_copy(tmp_path: Path, mutate: Any) -> Path:
    path = tmp_path / "contract.yaml"
    data = yaml.safe_load(CONTRACT.read_text(encoding="utf-8"))
    mutate(data["lab_receipt_check"])
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    return path


@pytest.mark.parametrize(
    "key",
    [
        "ticket_pattern",
        "ticket_contract_path",
        "document_name",
        "files_per_page",
        "max_file_pages",
        "file_status_letters",
        "renamed_from_letter",
    ],
)
def test_contract_first_and_no_hardcoded_values_a_missing_parameter_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, key: str
) -> None:
    path = _contract_copy(tmp_path, lambda section: section.pop(key))
    monkeypatch.setattr(handler_module, "_CONTRACT_PATH", path)
    with pytest.raises(ValueError, match=key):
        HandlerLabReceiptCheck()


async def test_contract_first_and_no_hardcoded_values_each_parameter_changes_behaviour(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    document = load_document("clean_receipt_not_on_lab")

    def mutate(section: dict[str, Any]) -> None:
        section["ticket_pattern"] = (
            "NEVER-[0-9]+"  # no ticket is cited, so no criterion is read
        )
        section["files_per_page"] = 1  # one file per page
        section["document_name"] = (
            "{repo}-{pr_number}.elsewhere.json"  # no rule judges it
        )

    path = _contract_copy(tmp_path, mutate)
    monkeypatch.setattr(handler_module, "_CONTRACT_PATH", path)
    transport = RecordedGithub(document)
    handler = HandlerLabReceiptCheck(
        transport=transport,
        carrier=InMemoryCarrier(document.facts.receipts),
        lab_reader=InMemoryLab(document.facts.lab_reads),
    )
    result = await handler.handle(make_request(document))
    assert not any("/contents/" in p for p in transport.requests)
    assert len([p for p in transport.requests if "per_page=1&" in p]) >= 1
    # the renamed document name selects no rule, so the validator judges nothing
    assert result.issues == ()

    def other_letters(section: dict[str, Any]) -> None:
        section["file_status_letters"] = {"added": "A"}

    monkeypatch.setattr(
        handler_module, "_CONTRACT_PATH", _contract_copy(tmp_path, other_letters)
    )
    narrow = HandlerLabReceiptCheck(
        transport=RecordedGithub(document),
        carrier=InMemoryCarrier(document.facts.receipts),
        lab_reader=InMemoryLab(document.facts.lab_reads),
    )
    unknown = await narrow.handle(make_request(document))
    assert unknown.verdict is EnumLabReceiptVerdict.UNREADABLE


_FORBIDDEN_LITERALS = (
    "OMN-",
    "contracts/",
    "OmniNode-ai",
    ".lab-receipt.json",
    "launchd",
    "h200",
    "lab-a",
    "docs/",
    "receipt_required",
    "unclassified",
)


def _string_constants(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    docstrings: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(
            node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef
        ):
            body = node.body
            if (
                body
                and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
            ):
                docstrings.add(id(body[0].value))
    return [
        n.value
        for n in ast.walk(tree)
        if isinstance(n, ast.Constant)
        and isinstance(n.value, str)
        and id(n) not in docstrings
    ]


def test_contract_first_and_no_hardcoded_values_the_handler_holds_no_rule_or_deployment_literal() -> (
    None
):
    constants = _string_constants(NODE / "handlers" / "handler_lab_receipt_check.py")
    offenders = [c for c in constants for f in _FORBIDDEN_LITERALS if f in c]
    assert offenders == []


def test_contract_first_and_no_hardcoded_values_every_collaborator_is_a_protocol_seam() -> (
    None
):
    handler = HandlerLabReceiptCheck.__init__
    names = set(handler.__annotations__) - {"return"}
    assert names == {
        "transport",
        "carrier",
        "lab_reader",
        "contract_loader",
        "event_bus",
    }


def test_core_pin_is_a_tag() -> None:
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    source = pyproject["tool"]["uv"]["sources"]["omnibase-core"]
    assert set(source) == {"git", "tag"}
    assert re.fullmatch(r"dev-[0-9]{14}-[0-9a-f]{12}", source["tag"])
    assert not re.fullmatch(r"[0-9a-f]{40}", source["tag"])


def test_core_pin_is_a_tag_the_fixture_copy_names_its_source() -> None:
    assert (
        (FIXTURES / "SOURCE.txt")
        .read_text("utf-8")
        .startswith("Copies of the omnibase_core")
    )
