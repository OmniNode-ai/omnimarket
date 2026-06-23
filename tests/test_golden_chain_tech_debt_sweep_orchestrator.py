# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden-chain guardrails for node_tech_debt_sweep_orchestrator [OMN-12212]."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from importlib import import_module
from importlib.metadata import entry_points
from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

from omnimarket.nodes.node_tech_debt_sweep_orchestrator.handlers.handler_tech_debt_sweep_orchestrator import (
    HandlerTechDebtSweepOrchestrator,
)
from omnimarket.nodes.node_tech_debt_sweep_orchestrator.models.model_tech_debt_sweep_request import (
    ModelTechDebtSweepRequest,
)

NODE_NAME = "node_tech_debt_sweep_orchestrator"
HANDLER_MODULE = (
    "omnimarket.nodes.node_tech_debt_sweep_orchestrator"
    ".handlers.handler_tech_debt_sweep_orchestrator"
)
HANDLER_CLASS = "HandlerTechDebtSweepOrchestrator"
REQUEST_MODULE = (
    "omnimarket.nodes.node_tech_debt_sweep_orchestrator"
    ".models.model_tech_debt_sweep_request"
)
REQUEST_CLASS = "ModelTechDebtSweepRequest"
RESULT_CLASS = "ModelTechDebtSweepResult"


class FakeLinearAdapter:
    def __init__(self, existing_keys: set[str] | None = None) -> None:
        self.existing_keys = existing_keys or set()
        self.epics: list[dict[str, Any]] = []
        self.tickets: list[dict[str, Any]] = []

    def open_dedup_keys(self) -> set[str]:
        return set(self.existing_keys)

    def create_epic(self, payload: dict[str, Any]) -> str:
        self.epics.append(payload)
        return f"EPIC-{len(self.epics)}"

    def create_ticket(self, payload: dict[str, Any]) -> str:
        self.tickets.append(payload)
        return f"OMN-TD-{len(self.tickets)}"


class FakeStaleIgnoreAdapter:
    def find_stale_type_ignores(self, repo_path: Path) -> list[Mapping[str, Any]]:
        return [
            {
                "path": repo_path / "src" / "app.py",
                "line_number": 3,
                "message": "unused type ignore",
            }
        ]


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _contract_path() -> Path:
    return _repo_root() / "src" / "omnimarket" / "nodes" / NODE_NAME / "contract.yaml"


def _write_repo(omni_home: Path, repo_name: str = "sample_repo") -> Path:
    repo = omni_home / repo_name
    src = repo / "src"
    tests = repo / "tests"
    src.mkdir(parents=True)
    tests.mkdir()
    (repo / "pyproject.toml").write_text("[project]\nname='sample-repo'\n")
    (src / "app.py").write_text(
        "from typing import Any\n"
        "value: Any = 1  # noqa: F841\n"
        "ignored = missing  # type: ignore[name-defined]\n"
        "# TODO: remove legacy branch\n",
        encoding="utf-8",
    )
    (tests / "test_app.py").write_text(
        "import pytest\n\n"
        "@pytest.mark.skip(reason='legacy fixture')\n"
        "def test_skip():\n"
        "    pass\n",
        encoding="utf-8",
    )
    return repo


@pytest.mark.unit
def test_contract_marks_node_implemented() -> None:
    raw = yaml.safe_load(_contract_path().read_text(encoding="utf-8"))

    assert raw["node_not_implemented"] is False
    assert raw["node_type"] == "orchestrator"
    assert raw["handler"]["module"] == HANDLER_MODULE
    assert raw["handler"]["class"] == HANDLER_CLASS
    assert raw["handler"]["input_model"] == f"{REQUEST_MODULE}.{REQUEST_CLASS}"
    assert raw["terminal_event"] == "onex.evt.omnimarket.tech-debt-sweep-completed.v1"


@pytest.mark.unit
def test_contract_declares_event_bus_surfaces() -> None:
    raw = yaml.safe_load(_contract_path().read_text(encoding="utf-8"))

    assert raw["handler_routing"]["routing_strategy"] == "operation_match"
    assert raw["handler_routing"]["handlers"] == [
        {
            "operation": "tech_debt_sweep",
            "handler": {
                "name": HANDLER_CLASS,
                "module": HANDLER_MODULE,
            },
        }
    ]
    eb = raw["event_bus"]
    assert eb["consumer_group"] == "omnimarket.tech_debt_sweep_orchestrator.consume.v1"
    assert "onex.cmd.omnimarket.tech-debt-sweep-start.v1" in eb["subscribe_topics"]
    assert "onex.evt.omnimarket.tech-debt-sweep-completed.v1" in eb["publish_topics"]
    assert "onex.dlq.omnimarket.tech-debt-sweep.v1" in eb["dlq_topics"]


@pytest.mark.unit
def test_entry_point_loads() -> None:
    eps = {ep.name: ep for ep in entry_points(group="onex.nodes")}

    loaded = eps[NODE_NAME].load()

    assert loaded.__name__ == f"omnimarket.nodes.{NODE_NAME}"


@pytest.mark.unit
def test_request_model_defaults_and_strict() -> None:
    mod = import_module(REQUEST_MODULE)
    ModelTechDebtSweepRequest = getattr(mod, REQUEST_CLASS)  # noqa: N806

    req = ModelTechDebtSweepRequest()
    assert req.repos == ()
    assert req.categories == ()
    assert req.dry_run is False
    assert req.omni_home == ""
    assert req.linear_team == "Omninode"
    assert req.linear_project == "Active Sprint"

    with pytest.raises(ValidationError):
        ModelTechDebtSweepRequest(unexpected_field=True)
    with pytest.raises(ValidationError):
        ModelTechDebtSweepRequest(categories=("unknown",))


@pytest.mark.unit
def test_result_model_is_strict() -> None:
    mod = import_module(REQUEST_MODULE)
    ModelCategoryResult = mod.ModelCategoryResult  # noqa: N806
    ModelTechDebtSweepResult = getattr(mod, RESULT_CLASS)  # noqa: N806

    cat = ModelCategoryResult(
        category="type-ignore",
        total_findings=10,
        new_findings=3,
        already_tracked=7,
        tickets_created=2,
    )
    result = ModelTechDebtSweepResult(
        repos_scanned=("omnibase_infra",),
        category_results=(cat,),
        total_findings=10,
        total_new_findings=3,
        total_tickets_created=2,
        skipped_duplicates=7,
        dry_run=False,
    )
    assert result.total_findings == 10
    assert result.total_tickets_created == 2
    assert result.repos_skipped_stale_ignores == ()
    assert result.summary == ""

    with pytest.raises(ValidationError):
        ModelTechDebtSweepResult(
            repos_scanned=("omnibase_infra",),
            category_results=(cat,),
            total_findings=10,
            total_new_findings=3,
            total_tickets_created=2,
            skipped_duplicates=7,
            dry_run=False,
            unexpected_field=True,
        )


@pytest.mark.unit
def test_handler_dry_run_scans_without_linear_mutation(tmp_path: Path) -> None:
    _write_repo(tmp_path)
    adapter = FakeLinearAdapter()

    result = HandlerTechDebtSweepOrchestrator(linear_adapter=adapter).handle(
        ModelTechDebtSweepRequest(
            omni_home=str(tmp_path),
            repos=("sample_repo",),
            categories=(
                "type-ignore",
                "noqa",
                "todo-fixme",
                "any-types",
                "skipped-tests",
            ),
            dry_run=True,
        )
    )

    counts = {item.category: item.total_findings for item in result.category_results}
    assert counts == {
        "type-ignore": 1,
        "noqa": 1,
        "todo-fixme": 1,
        "any-types": 2,
        "skipped-tests": 1,
    }
    assert result.total_new_findings == 6
    assert result.total_tickets_created == 0
    assert adapter.epics == []
    assert adapter.tickets == []


@pytest.mark.unit
def test_handler_live_creates_grouped_tickets_through_adapter(tmp_path: Path) -> None:
    _write_repo(tmp_path)
    adapter = FakeLinearAdapter()

    result = HandlerTechDebtSweepOrchestrator(linear_adapter=adapter).handle(
        ModelTechDebtSweepRequest(
            omni_home=str(tmp_path),
            repos=("sample_repo",),
            categories=("type-ignore", "todo-fixme"),
            dry_run=False,
            linear_project="Tech Debt Remediation",
        )
    )

    assert result.total_findings == 2
    assert result.total_new_findings == 2
    assert result.total_tickets_created == 2
    assert [epic["category"] for epic in adapter.epics] == [
        "type-ignore",
        "todo-fixme",
    ]
    assert {ticket["category"] for ticket in adapter.tickets} == {
        "type-ignore",
        "todo-fixme",
    }
    assert all(
        ticket["project"] == "Tech Debt Remediation" for ticket in adapter.tickets
    )
    assert all(ticket["parent"].startswith("EPIC-") for ticket in adapter.tickets)


@pytest.mark.unit
def test_handler_deduplicates_against_open_linear_keys(tmp_path: Path) -> None:
    _write_repo(tmp_path)
    adapter = FakeLinearAdapter(existing_keys=_type_ignore_key())

    result = HandlerTechDebtSweepOrchestrator(linear_adapter=adapter).handle(
        ModelTechDebtSweepRequest(
            omni_home=str(tmp_path),
            repos=("sample_repo",),
            categories=("type-ignore",),
            dry_run=False,
        )
    )

    assert result.total_findings == 1
    assert result.total_new_findings == 0
    assert result.skipped_duplicates == 1
    assert result.total_tickets_created == 0
    assert adapter.tickets == []


def _type_ignore_key() -> set[str]:
    line = "ignored = missing  # type: ignore[name-defined]"
    basis = "\n".join(
        ["type-ignore", "sample_repo", "src/app.py", "3", " ".join(line.split())]
    )
    return {hashlib.sha256(basis.encode("utf-8")).hexdigest()[:24]}


@pytest.mark.unit
def test_handler_requires_linear_adapter_for_live_new_findings(tmp_path: Path) -> None:
    _write_repo(tmp_path)

    with pytest.raises(RuntimeError, match="linear adapter required"):
        HandlerTechDebtSweepOrchestrator().handle(
            ModelTechDebtSweepRequest(
                omni_home=str(tmp_path),
                repos=("sample_repo",),
                categories=("type-ignore",),
                dry_run=False,
            )
        )


@pytest.mark.unit
def test_handler_skips_stale_ignores_without_native_analyzer(tmp_path: Path) -> None:
    _write_repo(tmp_path)

    result = HandlerTechDebtSweepOrchestrator().handle(
        ModelTechDebtSweepRequest(
            omni_home=str(tmp_path),
            repos=("sample_repo",),
            categories=("stale-ignores",),
            dry_run=True,
        )
    )

    assert result.total_findings == 0
    assert result.repos_skipped_stale_ignores == ("sample_repo",)


@pytest.mark.unit
def test_handler_uses_stale_ignore_adapter_when_available(tmp_path: Path) -> None:
    _write_repo(tmp_path)
    adapter = FakeLinearAdapter()

    result = HandlerTechDebtSweepOrchestrator(
        linear_adapter=adapter,
        stale_ignore_adapter=FakeStaleIgnoreAdapter(),
    ).handle(
        ModelTechDebtSweepRequest(
            omni_home=str(tmp_path),
            repos=("sample_repo",),
            categories=("stale-ignores",),
            dry_run=False,
        )
    )

    assert result.total_findings == 1
    assert result.total_tickets_created == 1
    assert adapter.tickets[0]["category"] == "stale-ignores"
