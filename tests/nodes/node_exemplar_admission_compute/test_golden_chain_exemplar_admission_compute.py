# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-21128: TC.5 admission facts through the contract and in-memory bus.

The good fixture supplies source and unaggregated check runs. Golden chain dispatches
that request through RuntimeLocal; error chain refuses a malformed commit before a result.
"""

from __future__ import annotations

import importlib
import json
from importlib.metadata import entry_points
from pathlib import Path
from typing import Any

import pytest
import yaml
from omnibase_core.enums.enum_workflow_result import EnumWorkflowResult
from pydantic import ValidationError

import omnimarket.nodes.node_exemplar_admission_compute as node_package
from omnimarket.nodes.node_exemplar_admission_compute.handlers.handler_exemplar_admission import (
    HandlerExemplarAdmission,
)
from omnimarket.nodes.node_exemplar_admission_compute.models.model_exemplar_admission import (
    DEFAULT_ADMISSION_RULES,
    ModelAdmissionDecision,
    ModelExemplarAdmissionRequest,
    ModelExemplarAdmissionResult,
    ModelExemplarCandidate,
    ModelTestReceipt,
)
from tests.runtime_local_compat import RuntimeLocal

pytestmark = pytest.mark.unit

NODE_DIR = Path(node_package.__file__).parent
COMMAND_TOPIC = "onex.cmd.omnimarket.exemplar-admission-requested.v1"
TERMINAL_TOPIC = "onex.evt.omnimarket.exemplar-admission-computed.v1"
COMMIT = "a" * 40
PACKAGE = "omnimarket.nodes.node_good"
MODULE = f"{PACKAGE}.handlers.handler_good"
MODELS = f"{PACKAGE}.models"
HANDLER = HandlerExemplarAdmission()
GOOD_SOURCE = f"""from pydantic import BaseModel
from {MODELS} import ModelGoodRequest, ModelGoodResult

class HandlerGood:
    def handle(self, request: ModelGoodRequest) -> ModelGoodResult:
        return ModelGoodResult()
"""


def candidate(**overrides: Any) -> dict[str, Any]:
    """A good compute node, with an older failed test run kept in the receipt."""
    return {
        "node_id": "node_good",
        "node_archetype": "compute",
        "contract_path": "src/omnimarket/nodes/node_good/contract.yaml",
        "handler_module": MODULE,
        "handler_class": "HandlerGood",
        "node_package": PACKAGE,
        "bindings": [
            {"handler_module": MODULE, "category": "canonical", "is_canonical": True}
        ],
        "sources": {
            MODULE: GOOD_SOURCE,
            MODELS: "from pydantic import BaseModel\nclass ModelGoodRequest(BaseModel):\n    pass\nclass ModelGoodResult(BaseModel):\n    pass\n",
        },
        "contract_valid": True,
        "unit_test_dir_present": True,
        "test_receipt": {
            "required_check_names": ["ci-summary", "tests"],
            "check_runs": [
                {
                    "id": 11,
                    "name": "tests",
                    "head_sha": COMMIT,
                    "status": "completed",
                    "conclusion": "success",
                },
                {
                    "id": 5,
                    "name": "tests",
                    "head_sha": COMMIT,
                    "status": "completed",
                    "conclusion": "failure",
                },
                {
                    "id": 10,
                    "name": "ci-summary",
                    "head_sha": COMMIT,
                    "status": "completed",
                    "conclusion": "success",
                },
            ],
            "collected_test_paths": ["tests/nodes/node_good/test_good.py::test_good"],
            "node_test_path": "tests/nodes/node_good",
        },
        **overrides,
    }


def request(**overrides: Any) -> dict[str, Any]:
    return {
        "repository": "omnimarket",
        "commit": COMMIT,
        "gate_version": "canonical-handler-shape.v1",
        "baseline_sha256": "b" * 64,
        "baseline_node_ids": [],
        "candidates": [candidate()],
        **overrides,
    }


def decide(
    good: dict[str, Any] | None = None, **overrides: Any
) -> ModelAdmissionDecision:
    return HANDLER.handle(
        ModelExemplarAdmissionRequest.model_validate(
            request(candidates=[candidate() if good is None else good], **overrides)
        )
    ).decisions[0]


def codes(decision: ModelAdmissionDecision) -> list[str]:
    return [violation.code for violation in decision.violations]


def source_candidate(source: str, **overrides: Any) -> dict[str, Any]:
    good = candidate(**overrides)
    good["sources"][MODULE] = source
    return good


def _contract() -> dict[str, Any]:
    return dict(yaml.safe_load((NODE_DIR / "contract.yaml").read_text()))


def test_contract_declares_topics_models_handler_and_entry_point() -> None:
    contract = _contract()
    assert contract["node_type"] == "compute"
    assert contract["runtime_dispatch"]["command_topic"] == COMMAND_TOPIC
    assert contract["event_bus"]["subscribe_topics"] == [COMMAND_TOPIC]
    assert contract["event_bus"]["publish_topics"] == [TERMINAL_TOPIC]
    assert contract["externally_consumed_topics"] == [TERMINAL_TOPIC]
    assert contract["terminal_event"] == TERMINAL_TOPIC
    assert (
        contract["handler_routing"]["handlers"][0]["operation"]
        == "decide_exemplar_admission"
    )
    binding = contract["handler"]
    handler_type = getattr(importlib.import_module(binding["module"]), binding["class"])
    assert issubclass(node_package.NodeExemplarAdmissionCompute, handler_type)
    for side, model in (
        ("input_model", ModelExemplarAdmissionRequest),
        ("output_model", ModelExemplarAdmissionResult),
    ):
        declared = getattr(
            importlib.import_module(contract[side]["module"]), contract[side]["name"]
        )
        assert declared is model
        assert binding[side] == f"{model.__module__}.{model.__name__}"
    registered = {e.name: e for e in entry_points(group="onex.nodes")}
    assert registered["node_exemplar_admission_compute"].load() is node_package


def test_good_candidate_is_admitted_with_receipt_and_unknown_coverage() -> None:
    result = decide()
    assert result.admitted
    assert result.rank == 1
    assert result.violations == []
    assert result.coverage_state == "unknown"
    assert result.coverage_percent is None
    assert result.handler_lines == len(GOOD_SOURCE.splitlines())
    assert result.receipt is not None
    assert result.receipt.model_dump() == {
        "gate": "canonical-handler-shape",
        "gate_version": "canonical-handler-shape.v1",
        "baseline_sha256": "b" * 64,
        "result": "pass",
        "commit": COMMIT,
        "check_run_ids": [10, 11],
    }


def test_known_coverage_is_preserved() -> None:
    result = decide(candidate(coverage_percent=87.5))
    assert result.admitted
    assert (result.coverage_state, result.coverage_percent) == ("known", 87.5)


def test_baseline_candidate_is_refused_for_exactly_in_baseline() -> None:
    result = decide(baseline_node_ids=["node_good"])
    assert codes(result) == ["in_baseline"]
    assert not result.admitted
    assert result.rank is None
    assert result.receipt is None


def test_gate_absent_repository_is_refused() -> None:
    assert codes(decide(repository="omnibase_spi")) == ["gate_absent_in_repository"]


def test_private_repository_is_refused() -> None:
    assert codes(decide(repository_private=True)) == ["gate_absent_in_repository"]


def test_first_noncanonical_binding_fails_gate() -> None:
    good = candidate()
    good["bindings"][0].update(
        is_canonical=False, category="phantom", detail="missing adapter"
    )
    result = decide(good)
    assert codes(result) == ["gate_failed"]
    assert result.violations[0].detail == "phantom: missing adapter"


def test_canonical_kwargs_stub_is_refused() -> None:
    result = decide(
        source_candidate(
            "class HandlerGood:\n    def handle(self, **kwargs) -> ModelGoodResult:\n        pass\n"
        )
    )
    assert "handle_kwargs" in codes(result)


def test_zero_argument_handle_is_refused() -> None:
    assert codes(
        decide(
            source_candidate(
                "class HandlerGood:\n    def handle(self) -> ModelGoodResult:\n        pass\n"
            )
        )
    ) == ["handle_zeroarg"]


def test_second_noncanonical_binding_does_not_fail_first_binding_gate() -> None:
    good = candidate()
    good["bindings"].append(
        {
            "handler_module": "other.handler",
            "category": "op_method",
            "is_canonical": False,
        }
    )
    result = decide(good)
    assert codes(result) == ["binding_non_canonical"]
    assert result.violations[0].detail == "binding[1] other.handler: op_method"


@pytest.mark.parametrize(
    "import_text", ["import psycopg", "from sqlalchemy import create_engine"]
)
def test_compute_database_import_is_impure(import_text: str) -> None:
    result = decide(source_candidate(import_text + "\n" + GOOD_SOURCE))
    assert codes(result) == ["impure_import"]
    assert result.violations[0].detail.startswith("database:")


@pytest.mark.parametrize(
    "import_text", ["import psycopg", "from sqlalchemy import create_engine"]
)
def test_effect_database_import_is_admitted(import_text: str) -> None:
    assert decide(
        source_candidate(import_text + "\n" + GOOD_SOURCE, node_archetype="effect")
    ).admitted


def test_transitive_relative_import_is_scanned() -> None:
    good = source_candidate("from .helpers import x\n" + GOOD_SOURCE)
    helpers = f"{PACKAGE}.handlers.helpers"
    good["sources"][helpers] = "import subprocess\nx = 1\n"
    result = decide(good)
    assert codes(result) == ["impure_import"]
    assert result.violations[0].detail == f"subprocess:subprocess in {helpers}"


def test_missing_intrapackage_import_source_is_refused() -> None:
    result = decide(source_candidate("from .helpers import x\n" + GOOD_SOURCE))
    assert codes(result) == ["import_source_missing"]
    assert result.violations[0].detail == f"{PACKAGE}.handlers.helpers"


def test_clock_call_is_impure() -> None:
    result = decide(
        source_candidate(
            "from datetime import datetime\n" + GOOD_SOURCE + "datetime.now()\n"
        )
    )
    assert codes(result) == ["impure_import"]
    assert result.violations[0].detail == f"clock:datetime.now in {MODULE}"


def test_datetime_annotation_is_pure() -> None:
    assert decide(
        source_candidate(
            "from datetime import datetime\nstamp: datetime\n" + GOOD_SOURCE
        )
    ).admitted


def test_newest_failed_run_on_commit_is_refused() -> None:
    good = candidate()
    good["test_receipt"]["check_runs"].append(
        {
            "id": 12,
            "name": "tests",
            "head_sha": COMMIT,
            "status": "completed",
            "conclusion": "failure",
        }
    )
    result = decide(good)
    assert codes(result) == ["required_check_not_success"]
    assert result.violations[0].detail == "tests: completed/failure run 12"


def test_missing_required_check_is_refused() -> None:
    good = candidate()
    good["test_receipt"]["required_check_names"].append("lint")
    result = decide(good)
    assert codes(result) == ["required_check_missing"]
    assert result.violations[0].detail == "lint"


def test_required_check_only_on_other_commit_is_refused() -> None:
    good = candidate()
    for run in good["test_receipt"]["check_runs"]:
        if run["name"] == "tests":
            run["head_sha"] = "c" * 40
    result = decide(good)
    assert codes(result) == ["receipt_run_other_commit"]
    assert result.violations[0].detail == "tests"


def test_newer_failure_on_other_commit_does_not_override_success() -> None:
    good = candidate()
    good["test_receipt"]["check_runs"].append(
        {
            "id": 100,
            "name": "tests",
            "head_sha": "c" * 40,
            "status": "completed",
            "conclusion": "failure",
        }
    )
    assert decide(good).admitted


def test_node_tests_must_be_collected() -> None:
    good = candidate()
    good["test_receipt"]["collected_test_paths"] = [
        "tests/nodes/node_good_other/test_other.py"
    ]
    assert codes(decide(good)) == ["node_tests_not_collected"]


def test_receipt_missing_skips_other_receipt_checks() -> None:
    assert codes(decide(candidate(test_receipt=None))) == ["test_receipt_missing"]


@pytest.mark.parametrize(
    "source",
    [
        'password = "hunter2"',
        'API_KEY: str = "abc"',
        'client(api_key="x")',
        'client.secret_key = "abc"',
    ],
)
def test_secret_literals_are_refused_without_exposing_values(source: str) -> None:
    result = decide(source_candidate(source + "\n" + GOOD_SOURCE))
    assert codes(result) == ["deny_secret_literal"]
    serialized = result.model_dump_json()
    assert "hunter2" not in serialized
    assert '"abc"' not in serialized
    assert MODULE in result.violations[0].detail


def test_bare_eval_is_refused() -> None:
    result = decide(source_candidate("eval(x)\n" + GOOD_SOURCE))
    assert codes(result) == ["deny_eval_exec"]
    assert result.violations[0].detail == f"eval in {MODULE}"


@pytest.mark.parametrize("archetype", ["compute", "effect", "orchestrator", "reducer"])
def test_pickle_is_refused_for_every_archetype(archetype: str) -> None:
    result = decide(
        source_candidate("import pickle\n" + GOOD_SOURCE, node_archetype=archetype)
    )
    assert codes(result) == ["deny_pickle"]


def test_path_arithmetic_and_model_dict_annotations_are_pure() -> None:
    good = source_candidate(
        'from pathlib import Path\np = Path("a") / "b"\n' + GOOD_SOURCE
    )
    good["sources"][MODELS] += (
        "\nfrom typing import Any\nclass ModelExtra(BaseModel):\n    field: dict[str, Any]\n"
    )
    assert decide(good).admitted


def test_all_violations_include_baseline_and_kwargs() -> None:
    result = decide(
        source_candidate(
            "class HandlerGood:\n    def handle(self, **kwargs):\n        pass\n"
        ),
        baseline_node_ids=["node_good"],
    )
    assert {"in_baseline", "handle_kwargs"} <= set(codes(result))
    assert codes(result) == [
        "in_baseline",
        "handle_kwargs",
        "handle_zeroarg",
        "handle_return_untyped",
    ]


def test_ranking_uses_lines_then_path_and_separate_archetypes() -> None:
    candidates = [
        source_candidate(
            GOOD_SOURCE + "\n# longer\n",
            node_id="long",
            contract_path="a/contract.yaml",
        ),
        candidate(node_id="tie_z", contract_path="z/contract.yaml"),
        candidate(node_id="tie_a", contract_path="a/contract.yaml"),
        candidate(node_id="effect", node_archetype="effect"),
        candidate(node_id="refused", contract_valid=False),
    ]
    result = HANDLER.handle(
        ModelExemplarAdmissionRequest.model_validate(request(candidates=candidates))
    )
    assert [d.node_id for d in result.decisions] == [c["node_id"] for c in candidates]
    assert result.admitted_by_archetype == {
        "compute": ["tie_a", "tie_z", "long"],
        "effect": ["effect"],
    }
    assert [d.rank for d in result.decisions] == [3, 2, 1, 1, None]
    assert (result.admitted_count, result.refused_count) == (4, 1)
    assert result.decisions[-1].receipt is None


@pytest.mark.parametrize(
    ("model", "extra"),
    [
        (ModelExemplarCandidate, {"passed": True}),
        (ModelExemplarCandidate, {"confidence": 0.9}),
        (ModelTestReceipt, {"passed": True}),
        (ModelTestReceipt, {"confidence": 0.9}),
    ],
)
def test_caller_asserted_success_fields_are_forbidden(
    model: type[ModelExemplarCandidate] | type[ModelTestReceipt], extra: dict[str, Any]
) -> None:
    payload = (
        candidate() if model is ModelExemplarCandidate else candidate()["test_receipt"]
    )
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        model.model_validate({**payload, **extra})


async def _run(tmp_path: Path, payload: dict[str, Any]) -> RuntimeLocal:
    input_path = tmp_path / "request.json"
    input_path.write_text(json.dumps(payload))
    runtime = RuntimeLocal(
        workflow_path=NODE_DIR / "contract.yaml",
        input_path=input_path,
        state_root=tmp_path / "state",
        backend_overrides={"event_bus": "inmemory"},
        timeout=10,
    )
    await runtime.run_async()
    return runtime


@pytest.mark.asyncio
async def test_golden_chain_over_the_bus_admits_good_candidate(tmp_path: Path) -> None:
    payload = request()
    runtime = await _run(tmp_path, payload)
    result = runtime.handler_result
    assert isinstance(result, ModelExemplarAdmissionResult)
    assert result == HANDLER.handle(
        ModelExemplarAdmissionRequest.model_validate(payload)
    )
    assert result.decisions[0].admitted
    assert result.decisions[0].rank == 1
    assert result.decisions[0].receipt is not None
    assert result.decisions[0].receipt.check_run_ids == [10, 11]
    assert (
        ModelExemplarAdmissionResult.model_validate_json(result.model_dump_json())
        == result
    )


@pytest.mark.asyncio
async def test_error_chain_over_the_bus_fails_without_result(tmp_path: Path) -> None:
    malformed = request(commit="abc")
    with pytest.raises(ValidationError, match="commit"):
        ModelExemplarAdmissionRequest.model_validate(malformed)
    runtime = await _run(tmp_path, malformed)
    assert runtime.handler_result is None
    (tmp_path / "state2").mkdir()
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps(malformed))
    refused = RuntimeLocal(
        workflow_path=NODE_DIR / "contract.yaml",
        input_path=bad,
        state_root=tmp_path / "state2",
        backend_overrides={"event_bus": "inmemory"},
        timeout=10,
    )
    assert await refused.run_async() is EnumWorkflowResult.FAILED
    assert refused.handler_result is None


def test_no_bindings_is_refused_without_indexing_empty_list() -> None:
    assert codes(decide(candidate(bindings=[]))) == ["no_bindings"]


def test_missing_handler_source_skips_all_ast_checks() -> None:
    good = candidate(sources={MODELS: 'import pickle\npassword = "hidden"\n!!!'})
    assert codes(decide(good)) == ["handler_source_missing"]


def test_unparseable_module_is_reported_and_other_modules_are_still_scanned() -> None:
    good = candidate()
    good["sources"][MODELS] = "not valid python!"
    good["sources"][f"{PACKAGE}.unused"] = "import pickle\n"
    result = decide(good)
    assert codes(result) == ["handler_unparseable", "deny_pickle"]
    assert result.violations[0].detail == MODELS


def test_unparseable_handler_skips_handle_shape_checks() -> None:
    result = decide(source_candidate("class HandlerGood: !"))
    assert codes(result) == ["handler_unparseable"]
    assert result.violations[0].detail == MODULE


def test_missing_handle_is_refused() -> None:
    assert codes(decide(source_candidate("class HandlerGood:\n    pass\n"))) == [
        "handle_not_found"
    ]


def test_inherited_async_handle_is_found_depth_first_with_cycles() -> None:
    good = source_candidate("class HandlerGood(Loop, Right):\n    pass\n")
    good["sources"][f"{PACKAGE}.bases"] = """class Loop(HandlerGood):
    pass
class Right(Base):
    pass
class Base:
    async def handle(self, request: ModelGoodRequest) -> ModelGoodResult:
        pass
"""
    assert decide(good).admitted


def test_inheritance_cycle_without_handle_is_refused() -> None:
    good = source_candidate(
        "class HandlerGood(Loop):\n    pass\nclass Loop(HandlerGood):\n    pass\n"
    )
    assert codes(decide(good)) == ["handle_not_found"]


@pytest.mark.parametrize(
    ("signature", "expected"),
    [
        ("self, *args", ["handle_varargs"]),
        (
            "self, first: ModelGoodRequest, second: ModelGoodRequest",
            ["handle_multi_positional"],
        ),
        ("self, request: ModelGoodRequest, *, required: str", ["handle_keyword_only"]),
        ("self, request", ["handle_param_untyped"]),
        ("self, request: ModelGoodRequest, *, optional: str = 'x'", []),
        ("self, request: ModelGoodRequest, /", []),
    ],
)
def test_handle_parameter_shape(signature: str, expected: list[str]) -> None:
    good = source_candidate(
        f"class HandlerGood:\n    def handle({signature}) -> ModelGoodResult:\n        pass\n"
    )
    assert codes(decide(good)) == expected


@pytest.mark.parametrize(
    "annotation",
    [
        "Any",
        "object",
        "dict",
        "Dict",
        "list",
        "List",
        "None",
        "typing.Any",
        "typing.Dict",
        "dict[str, str]",
        "Dict[str, str]",
        "typing.Dict[str, str]",
        "list[str]",
        "Mapping[str, str]",
    ],
)
def test_untyped_handle_annotations_are_refused(annotation: str) -> None:
    source = f"class HandlerGood:\n    def handle(self, request: {annotation}) -> {annotation}:\n        pass\n"
    assert codes(decide(source_candidate(source))) == [
        "handle_param_untyped",
        "handle_return_untyped",
    ]


def test_type_checking_imports_are_ignored_but_else_imports_are_scanned() -> None:
    source = (
        """from typing import TYPE_CHECKING
if TYPE_CHECKING:
    import psycopg
    from .missing import TypeOnly
else:
    import httpx
"""
        + GOOD_SOURCE
    )
    result = decide(source_candidate(source))
    assert codes(result) == ["impure_import"]
    assert result.violations[0].detail == f"network:httpx in {MODULE}"


def test_typing_type_checking_and_module_prefix_matching() -> None:
    source = (
        """import typing
if typing.TYPE_CHECKING:
    import subprocess
import http.client
import requests_extra
"""
        + GOOD_SOURCE
    )
    result = decide(source_candidate(source))
    assert codes(result) == ["impure_import"]
    assert result.violations[0].detail == f"network:http.client in {MODULE}"


def test_package_init_relative_import_and_from_package_submodule_are_scanned() -> None:
    good = source_candidate(f"from {PACKAGE} import helpers\n" + GOOD_SOURCE)
    good["sources"][f"{PACKAGE}.helpers"] = "from . import nested\n"
    good["sources"][PACKAGE] = "from .danger import x\n"
    good["sources"][f"{PACKAGE}.nested"] = "from . import helpers\n"
    good["sources"][f"{PACKAGE}.danger"] = "import redis\nx = 1\n"
    result = decide(good)
    assert codes(result) == ["impure_import"]
    assert result.violations[0].detail == f"database:redis in {PACKAGE}.danger"


def test_unreachable_module_impurity_is_not_scanned_but_denies_are() -> None:
    good = candidate()
    good["sources"][f"{PACKAGE}.unused"] = (
        'import subprocess\nexec(text)\npassword = "hidden"\n'
    )
    assert codes(decide(good)) == ["deny_secret_literal", "deny_eval_exec"]


def test_odd_deny_targets_and_nonstring_values_do_not_raise() -> None:
    source = (
        """password = 123
password: str
api_key = None
secret_key = ""
values["password"] = "value"
client(password=123, **options)
cursor.execute(sql)
re.compile(pattern)
"""
        + GOOD_SOURCE
    )
    assert decide(source_candidate(source)).admitted


def test_contract_test_directory_and_empty_required_checks_all_refuse() -> None:
    good = candidate(contract_valid=False, unit_test_dir_present=False)
    good["test_receipt"]["required_check_names"] = []
    good["test_receipt"]["collected_test_paths"] = []
    assert codes(decide(good)) == [
        "contract_invalid",
        "unit_test_dir_missing",
        "receipt_no_required_checks",
        "node_tests_not_collected",
    ]


def test_receipt_failures_are_ordered_by_required_name() -> None:
    good = candidate()
    good["test_receipt"]["required_check_names"] = ["z-missing", "tests", "a-other"]
    good["test_receipt"]["check_runs"].append(
        {
            "id": 20,
            "name": "a-other",
            "head_sha": "c" * 40,
            "status": "completed",
            "conclusion": "success",
        }
    )
    good["test_receipt"]["check_runs"][0]["status"] = "in_progress"
    result = decide(good)
    assert codes(result) == [
        "receipt_run_other_commit",
        "required_check_not_success",
        "required_check_missing",
    ]
    assert result.violations[1].detail == "tests: in_progress/success run 11"


@pytest.mark.parametrize("trailing_slash", [False, True])
def test_exact_node_test_directory_or_child_is_collected(trailing_slash: bool) -> None:
    good = candidate()
    receipt = good["test_receipt"]
    if trailing_slash:
        receipt["node_test_path"] += "/"
    else:
        receipt["collected_test_paths"] = [receipt["node_test_path"]]
    assert decide(good).admitted


def test_rules_are_declared_versioned_data_and_can_be_overridden() -> None:
    rules = DEFAULT_ADMISSION_RULES.model_copy(
        update={
            "rules_version": "custom.v2",
            "pure_import_list_version": "custom-imports.v2",
            "impure_import_modules": {"custom": ("pydantic",)},
        }
    )
    result = HANDLER.handle(
        ModelExemplarAdmissionRequest.model_validate(request(rules=rules))
    )
    assert result.rules_version == "custom.v2"
    assert result.pure_import_list_version == "custom-imports.v2"
    assert all(
        v.detail.startswith("custom:pydantic") for v in result.decisions[0].violations
    )
    assert len(result.decisions[0].violations) == 2
    assert DEFAULT_ADMISSION_RULES.rules_version == "exemplar-admission-rules.v1"


@pytest.mark.parametrize("coverage", [-1, 101, float("nan"), float("inf")])
def test_coverage_must_be_a_real_percentage(coverage: float) -> None:
    with pytest.raises(ValidationError, match="coverage_percent"):
        ModelExemplarCandidate.model_validate(candidate(coverage_percent=coverage))


def test_empty_candidate_list_has_no_archetypes_or_counts() -> None:
    result = HANDLER.handle(
        ModelExemplarAdmissionRequest.model_validate(request(candidates=[]))
    )
    assert result.decisions == []
    assert result.admitted_by_archetype == {}
    assert (result.admitted_count, result.refused_count) == (0, 0)
