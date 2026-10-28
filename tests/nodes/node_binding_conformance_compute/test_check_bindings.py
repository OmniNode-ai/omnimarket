# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""check_bindings: nothing a node ships assumes one deployment's setup.

Fixtures use invented names only. The table, the concrete bindings and the checked set are data
the caller supplies (``data/binding_table.yaml`` for the repository's own run).
"""

from __future__ import annotations

import builtins
import importlib
import os
import pathlib
import textwrap
import tomllib
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

from omnimarket.adapters.codex.local_runtime_dispatch import _resolve_node_route
from omnimarket.models.binding_conformance.enum_binding_rule import EnumBindingRule
from omnimarket.models.binding_conformance.enum_binding_source_kind import (
    EnumBindingSourceKind,
)
from omnimarket.models.binding_conformance.enum_private_name_kind import (
    EnumPrivateNameKind,
)
from omnimarket.models.binding_conformance.model_binding_conformance_request import (
    ModelBindingConformanceRequest,
)
from omnimarket.models.binding_conformance.model_binding_conformance_result import (
    ModelBindingConformanceResult,
)
from omnimarket.models.binding_conformance.model_binding_protocol_table import (
    ModelBindingProtocolTable,
)
from omnimarket.models.binding_conformance.model_binding_source import (
    ModelBindingSource,
)
from omnimarket.models.binding_conformance.model_concrete_binding import (
    ModelConcreteBinding,
)
from omnimarket.models.binding_conformance.model_private_name import ModelPrivateName
from omnimarket.nodes.node_binding_conformance_compute.handlers.handler_binding_conformance import (
    HandlerBindingConformance,
)

pytestmark = pytest.mark.unit

REPO = Path(__file__).resolve().parents[3]
NODES = REPO / "src" / "omnimarket" / "nodes"
NODE = "node_binding_conformance_compute"
DATA = yaml.safe_load(
    (Path(__file__).parent / "data" / "binding_table.yaml").read_text(encoding="utf-8")
)

BUS_REF = "fixture_core.protocols:ProtocolBus"
CLK_REF = "fixture_core.protocols:ProtocolClock"
LED_REF = "fixture_core.protocols:ProtocolLedger"
# Invented names standing in for a deployment's overlay values.
INVENTED_REPO = "acme-widgets"
INVENTED_HOST = "box-17.fixture.invalid"
INVENTED_ORG = "acme-fixture-org"
PRIVATE_NAMES = (
    ModelPrivateName(kind=EnumPrivateNameKind.REPOSITORY, value=INVENTED_REPO),
    ModelPrivateName(kind=EnumPrivateNameKind.HOST, value=INVENTED_HOST),
    ModelPrivateName(kind=EnumPrivateNameKind.ORGANIZATION, value=INVENTED_ORG),
)

TABLE = ModelBindingProtocolTable.model_validate(
    {
        "protocols": [
            {"code": "BUS", "protocol": BUS_REF},
            {"code": "CLK", "protocol": CLK_REF},
            {"code": "LED", "protocol": LED_REF},
        ],
        "rows": [
            {"node": "node_fixture_effect", "protocols": ["BUS", "CLK"]},
            {"node": "node_fixture_pure_compute", "protocols": []},
        ],
    }
)
CONCRETE = (
    ModelConcreteBinding(
        code="BUS",
        modules=("fixture_kafka",),
        classes=("FixtureKafkaBus",),
        allowed_path_prefixes=("adapters/",),
    ),
)
PURE_BANNED = ("fixture_pkg.overlay_reader", "fixture_contract_loader")


def _contract(*refs: str) -> str:
    entries = [
        {"name": ref.split(":")[1], "protocol": ref, "required": True} for ref in refs
    ]
    return yaml.safe_dump(
        {
            "name": "node_fixture",
            "node_type": "effect",
            "protocol_dependencies": entries,
        }
    )


def _src(
    node: str, kind: EnumBindingSourceKind, text: str, path: str | None = None
) -> ModelBindingSource:
    default = {
        EnumBindingSourceKind.CONTRACT: "contract.yaml",
        EnumBindingSourceKind.HANDLER: "handlers/handler.py",
        EnumBindingSourceKind.MODEL: "models/model.py",
    }[kind]
    return ModelBindingSource(
        node=node, path=path or default, kind=kind, text=textwrap.dedent(text)
    )


CLEAN_EFFECT = textwrap.dedent(
    """\
    from fixture_core.protocols import ProtocolBus, ProtocolClock


    class Handler:
        def __init__(self, bus: ProtocolBus, clock: ProtocolClock) -> None:
            self._bus = bus
            self._clock = clock
    """
)
CLEAN_PURE = textwrap.dedent(
    """\
    class Handler:
        def handle(self, request: object) -> object:
            return request
    """
)


def _effect_sources(handler: str = CLEAN_EFFECT) -> tuple[ModelBindingSource, ...]:
    return (
        _src(
            "node_fixture_effect",
            EnumBindingSourceKind.CONTRACT,
            _contract(BUS_REF, CLK_REF),
        ),
        _src("node_fixture_effect", EnumBindingSourceKind.HANDLER, handler),
    )


def _pure_sources(
    handler: str = CLEAN_PURE, model: str = "class M:\n    pass\n"
) -> tuple[ModelBindingSource, ...]:
    return (
        _src("node_fixture_pure_compute", EnumBindingSourceKind.CONTRACT, _contract()),
        _src("node_fixture_pure_compute", EnumBindingSourceKind.HANDLER, handler),
        _src("node_fixture_pure_compute", EnumBindingSourceKind.MODEL, model),
    )


def _check(
    nodes: tuple[str, ...],
    sources: tuple[ModelBindingSource, ...],
    *,
    table: ModelBindingProtocolTable = TABLE,
    names: tuple[ModelPrivateName, ...] = PRIVATE_NAMES,
) -> ModelBindingConformanceResult:
    return HandlerBindingConformance().handle(
        ModelBindingConformanceRequest(
            checked_nodes=nodes,
            protocol_table=table,
            concrete_bindings=CONCRETE,
            pure_forbidden_modules=PURE_BANNED,
            private_names=names,
            sources=sources,
        )
    )


def _rules(result: ModelBindingConformanceResult) -> set[EnumBindingRule]:
    return {i.rule for i in result.issues}


def _lines(result: ModelBindingConformanceResult, rule: EnumBindingRule) -> list[int]:
    return sorted(i.line for i in result.issues if i.rule is rule)


# --- positive controls ---------------------------------------------------------------------


def test_a_conforming_effect_and_a_conforming_pure_node_pass() -> None:
    result = _check(
        ("node_fixture_effect", "node_fixture_pure_compute"),
        _effect_sources() + _pure_sources(),
    )
    assert result.issues == ()
    assert result.conforming is True
    assert result.checked_nodes == ("node_fixture_effect", "node_fixture_pure_compute")


# --- AC1: a concrete bus adapter ------------------------------------------------------------


def test_effect_that_constructs_a_concrete_bus_adapter_fails_naming_the_line() -> None:
    handler = """\
        from fixture_core.protocols import ProtocolBus, ProtocolClock
        from fixture_kafka import FixtureKafkaBus


        class Handler:
            def __init__(self, clock: ProtocolClock) -> None:
                self._clock = clock
                self._bus: ProtocolBus = FixtureKafkaBus()
        """
    result = _check(("node_fixture_effect",), _effect_sources(handler))
    assert result.conforming is False
    assert _lines(result, EnumBindingRule.CONCRETE_BINDING) == [2, 8]
    construct = next(i for i in result.issues if i.line == 8)
    assert construct.node == "node_fixture_effect"
    assert construct.path == "handlers/handler.py"
    assert "FixtureKafkaBus" in construct.detail
    assert "BUS" in construct.detail


def test_a_concrete_binding_is_allowed_in_its_own_adapter_path() -> None:
    adapter = _src(
        "node_fixture_effect",
        EnumBindingSourceKind.HANDLER,
        "from fixture_kafka import FixtureKafkaBus\nbus = FixtureKafkaBus()\n",
        path="adapters/bus.py",
    )
    result = _check(("node_fixture_effect",), (*_effect_sources(), adapter))
    assert result.issues == ()


# --- AC2: a protocol the row requires, and one it does not give -----------------------------


def test_contract_missing_a_protocol_its_row_requires_fails_naming_the_protocol() -> (
    None
):
    sources = (
        _src("node_fixture_effect", EnumBindingSourceKind.CONTRACT, _contract(BUS_REF)),
        _src("node_fixture_effect", EnumBindingSourceKind.HANDLER, CLEAN_EFFECT),
    )
    result = _check(("node_fixture_effect",), sources)
    assert result.conforming is False
    (issue,) = result.issues
    assert issue.rule is EnumBindingRule.CONTRACT_MISSING_PROTOCOL
    assert "CLK" in issue.detail
    assert CLK_REF in issue.detail


def test_contract_declaring_a_protocol_its_row_does_not_give_fails() -> None:
    sources = (
        _src(
            "node_fixture_effect",
            EnumBindingSourceKind.CONTRACT,
            _contract(BUS_REF, CLK_REF, LED_REF),
        ),
        _src("node_fixture_effect", EnumBindingSourceKind.HANDLER, CLEAN_EFFECT),
    )
    result = _check(("node_fixture_effect",), sources)
    (issue,) = result.issues
    assert issue.rule is EnumBindingRule.CONTRACT_EXTRA_PROTOCOL
    assert "LED" in issue.detail


def test_contract_declaring_a_protocol_the_table_does_not_know_fails() -> None:
    sources = (
        _src(
            "node_fixture_effect",
            EnumBindingSourceKind.CONTRACT,
            _contract(BUS_REF, CLK_REF, "elsewhere:ProtocolOther"),
        ),
        _src("node_fixture_effect", EnumBindingSourceKind.HANDLER, CLEAN_EFFECT),
    )
    result = _check(("node_fixture_effect",), sources)
    (issue,) = result.issues
    assert issue.rule is EnumBindingRule.CONTRACT_UNKNOWN_PROTOCOL
    assert "elsewhere:ProtocolOther" in issue.detail


def test_a_pure_node_that_declares_a_protocol_fails() -> None:
    sources = (
        _src(
            "node_fixture_pure_compute",
            EnumBindingSourceKind.CONTRACT,
            _contract(BUS_REF),
        ),
        _src("node_fixture_pure_compute", EnumBindingSourceKind.HANDLER, CLEAN_PURE),
    )
    result = _check(("node_fixture_pure_compute",), sources)
    assert _rules(result) == {EnumBindingRule.CONTRACT_EXTRA_PROTOCOL}


@pytest.mark.parametrize(
    ("node", "sources", "rule"),
    [
        ("node_without_a_row", _effect_sources(), EnumBindingRule.NO_TABLE_ROW),
        ("node_fixture_effect", _effect_sources()[1:], EnumBindingRule.NO_CONTRACT),
        ("node_fixture_effect", _effect_sources()[:1], EnumBindingRule.NO_SOURCE),
    ],
)
def test_a_checked_node_with_no_row_contract_or_source_fails_rather_than_passing_empty(
    node: str, sources: tuple[ModelBindingSource, ...], rule: EnumBindingRule
) -> None:
    result = _check((node,), sources)
    assert result.conforming is False
    assert rule in _rules(result)


def test_an_unreadable_contract_fails() -> None:
    sources = (
        _src(
            "node_fixture_effect",
            EnumBindingSourceKind.CONTRACT,
            "protocol_dependencies: [1",
        ),
        _src("node_fixture_effect", EnumBindingSourceKind.HANDLER, CLEAN_EFFECT),
    )
    result = _check(("node_fixture_effect",), sources)
    assert _rules(result) == {EnumBindingRule.CONTRACT_UNREADABLE}


def test_the_table_refuses_a_row_naming_a_protocol_it_does_not_define() -> None:
    with pytest.raises(ValidationError):
        ModelBindingProtocolTable.model_validate(
            {
                "protocols": [{"code": "BUS", "protocol": BUS_REF}],
                "rows": [{"node": "n", "protocols": ["NOPE"]}],
            }
        )


# --- AC3: an invented repository name, host name, or an os.environ read --------------------


def test_handler_with_an_invented_repository_name_fails() -> None:
    handler = CLEAN_EFFECT + f'\n\nTARGET = "{INVENTED_REPO}"\n'
    result = _check(("node_fixture_effect",), _effect_sources(handler))
    assert _rules(result) == {EnumBindingRule.PRIVATE_NAME}
    (issue,) = result.issues
    assert issue.line == 10
    assert "repository" in issue.detail
    assert INVENTED_REPO not in issue.detail


def test_handler_with_an_invented_host_name_fails() -> None:
    handler = CLEAN_EFFECT + f"\n\n# runs on {INVENTED_HOST}\n"
    result = _check(("node_fixture_effect",), _effect_sources(handler))
    assert _rules(result) == {EnumBindingRule.PRIVATE_NAME}
    assert "host" in result.issues[0].detail
    assert INVENTED_HOST not in result.issues[0].detail


def test_handler_that_reads_os_environ_fails_naming_the_line() -> None:
    handler = CLEAN_EFFECT + '\n\nimport os\n\nvalue = os.environ["SOME_SETTING"]\n'
    result = _check(("node_fixture_effect",), _effect_sources(handler))
    assert _rules(result) == {EnumBindingRule.ENV_READ}
    assert _lines(result, EnumBindingRule.ENV_READ) == [12]


@pytest.mark.parametrize(
    "snippet",
    [
        "import os\nv = os.getenv('X')\n",
        "import os as o\nv = o.environ.get('X')\n",
        "from os import environ\nv = environ['X']\n",
        "from os import getenv\nv = getenv('X')\n",
    ],
)
def test_every_spelling_of_an_environment_read_fails(snippet: str) -> None:
    result = _check(
        ("node_fixture_effect",), _effect_sources(CLEAN_EFFECT + "\n" + snippet)
    )
    assert EnumBindingRule.ENV_READ in _rules(result)


def test_private_names_apply_to_the_contract_and_the_models_too() -> None:
    sources = (
        _src(
            "node_fixture_pure_compute",
            EnumBindingSourceKind.CONTRACT,
            _contract() + f"description: for {INVENTED_ORG}\n",
        ),
        _src("node_fixture_pure_compute", EnumBindingSourceKind.HANDLER, CLEAN_PURE),
        _src(
            "node_fixture_pure_compute",
            EnumBindingSourceKind.MODEL,
            f'DEFAULT = "{INVENTED_REPO}"\n',
        ),
    )
    result = _check(("node_fixture_pure_compute",), sources)
    assert {i.path for i in result.issues} == {"contract.yaml", "models/model.py"}
    assert _rules(result) == {EnumBindingRule.PRIVATE_NAME}


def test_a_private_name_is_matched_whole_not_inside_a_longer_word() -> None:
    handler = CLEAN_EFFECT + f"\n\nOTHER = 'x{INVENTED_REPO}y'\n"
    assert _check(("node_fixture_effect",), _effect_sources(handler)).issues == ()


def test_private_names_come_from_the_request_not_from_the_node() -> None:
    handler = CLEAN_EFFECT + f'\n\nTARGET = "{INVENTED_REPO}"\n'
    sources = _effect_sources(handler)
    assert _check(("node_fixture_effect",), sources, names=()).conforming is True
    other = (ModelPrivateName(kind=EnumPrivateNameKind.REPOSITORY, value="other-repo"),)
    assert _check(("node_fixture_effect",), sources, names=other).conforming is True
    assert _check(("node_fixture_effect",), sources).conforming is False


@pytest.mark.parametrize(
    ("literal", "rule"),
    [
        ("https://svc.fixture.invalid/v1", EnumBindingRule.URL_LITERAL),
        ("tcp://127.0.0.1:9000", EnumBindingRule.URL_LITERAL),
        ("/srv/fixture/ledger.md", EnumBindingRule.PATH_LITERAL),
        ("~/fixture/state", EnumBindingRule.PATH_LITERAL),
    ],
)
def test_handler_with_a_url_or_path_literal_fails(
    literal: str, rule: EnumBindingRule
) -> None:
    handler = CLEAN_EFFECT + f'\n\nLOCATION = "{literal}"\n'
    result = _check(("node_fixture_effect",), _effect_sources(handler))
    assert _rules(result) == {rule}
    assert _lines(result, rule) == [10]


def test_an_fstring_is_read_whole_not_by_its_fragments() -> None:
    quiet = CLEAN_EFFECT + "\n\nrate = 1.5\nlabel = f'{rate:.3f}/h'\n"
    assert _check(("node_fixture_effect",), _effect_sources(quiet)).issues == ()
    url = CLEAN_EFFECT + "\n\nhost = 'h'\nendpoint = f'https://{host}/v1'\n"
    result = _check(("node_fixture_effect",), _effect_sources(url))
    assert _rules(result) == {EnumBindingRule.URL_LITERAL}
    path = CLEAN_EFFECT + "\n\nname = 'n'\nlocation = f'/srv/{name}'\n"
    result = _check(("node_fixture_effect",), _effect_sources(path))
    assert _rules(result) == {EnumBindingRule.PATH_LITERAL}


def test_a_docstring_that_shows_a_url_is_not_a_literal_in_use() -> None:
    handler = '"""Talks to https://svc.fixture.invalid in the docs."""\n' + CLEAN_EFFECT
    assert _check(("node_fixture_effect",), _effect_sources(handler)).issues == ()


def test_a_handler_that_does_not_parse_fails() -> None:
    result = _check(("node_fixture_effect",), _effect_sources("def broken(:\n"))
    assert EnumBindingRule.SOURCE_UNPARSEABLE in _rules(result)


# --- AC4: purity ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "imports",
    [
        "from fixture_pkg.overlay_reader import load_structured_overlay\n",
        "import fixture_contract_loader\n",
        "from fixture_contract_loader.loader import load_contract\n",
        "from fixture_pkg import overlay_reader\n",
    ],
)
def test_a_pure_handler_or_model_that_imports_the_overlay_reader_or_loader_fails(
    imports: str,
) -> None:
    for sources in (
        _pure_sources(handler=imports + CLEAN_PURE),
        _pure_sources(model=imports),
    ):
        result = _check(("node_fixture_pure_compute",), sources)
        assert _rules(result) == {EnumBindingRule.PURE_LOADS_CONFIGURATION}
        assert result.issues[0].line == 1


def test_an_effect_may_import_what_a_pure_node_may_not() -> None:
    handler = (
        "from fixture_pkg.overlay_reader import load_structured_overlay\n"
        + CLEAN_EFFECT
    )
    assert _check(("node_fixture_effect",), _effect_sources(handler)).issues == ()


LOADS_ITS_OWN_OVERLAY = textwrap.dedent(
    """\
    from fixture_pkg.overlay_reader import load_structured_overlay


    class Handler:
        def handle(self, request: object) -> object:
            return load_structured_overlay("limits", dict)
    """
)


def test_a_fixture_handler_that_loads_its_own_overlay_fails() -> None:
    result = _check(
        ("node_fixture_pure_compute",), _pure_sources(handler=LOADS_ITS_OWN_OVERLAY)
    )
    assert result.conforming is False
    assert EnumBindingRule.PURE_LOADS_CONFIGURATION in _rules(result)


@contextmanager
def _guard_on_filesystem_and_environment_reads() -> Iterator[None]:
    """Raise on any open, stat, listing or environment read until the block ends."""

    def refuse(*_a: object, **_k: object) -> Any:
        raise AssertionError("filesystem or environment read under the guard")

    class RefusingEnviron(dict[str, str]):
        def __getitem__(self, key: str) -> str:
            return refuse()

        def get(self, *_a: object, **_k: object) -> Any:
            return refuse()

        def __contains__(self, key: object) -> bool:
            return refuse()

    patched: list[tuple[Any, str, Any]] = [
        (builtins, "open", refuse),
        (os, "stat", refuse),
        (os, "listdir", refuse),
        (os, "scandir", refuse),
        (os, "getenv", refuse),
        (os, "environ", RefusingEnviron()),
        (pathlib.Path, "open", refuse),
        (pathlib.Path, "read_text", refuse),
        (pathlib.Path, "read_bytes", refuse),
        (pathlib.Path, "exists", refuse),
        (pathlib.Path, "stat", refuse),
        (pathlib.Path, "iterdir", refuse),
    ]
    saved = [(owner, name, getattr(owner, name)) for owner, name, _ in patched]
    for owner, name, replacement in patched:
        setattr(owner, name, replacement)
    try:
        yield
    finally:
        for owner, name, original in saved:
            setattr(owner, name, original)


def test_the_guard_does_stop_a_handler_that_reads_its_own_overlay_or_environment() -> (
    None
):
    """Positive control: a read under the guard raises, so a pass below means no read happened."""
    with _guard_on_filesystem_and_environment_reads():
        with pytest.raises(AssertionError):
            Path("anything.yaml").read_text()
        with pytest.raises(AssertionError):
            os.environ.get("ANY")


def test_the_check_runs_under_the_guard_with_injected_configuration() -> None:
    request = ModelBindingConformanceRequest(
        checked_nodes=("node_fixture_effect", "node_fixture_pure_compute"),
        protocol_table=TABLE,
        concrete_bindings=CONCRETE,
        pure_forbidden_modules=PURE_BANNED,
        private_names=PRIVATE_NAMES,
        sources=_effect_sources() + _pure_sources(),
    )
    handler = HandlerBindingConformance()
    with _guard_on_filesystem_and_environment_reads():
        guarded = handler.handle(request)
    assert guarded.conforming is True
    assert guarded.model_dump_json() == handler.handle(request).model_dump_json()


# --- AC5: the repository's own contracts ---------------------------------------------------


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _real_sources(node: str) -> tuple[ModelBindingSource, ...]:
    root = NODES / node
    sources = [
        ModelBindingSource(
            node=node,
            path=f"src/omnimarket/nodes/{node}/contract.yaml",
            kind=EnumBindingSourceKind.CONTRACT,
            text=_read(root / "contract.yaml"),
        )
    ]
    files: list[tuple[EnumBindingSourceKind, Path]] = [
        (EnumBindingSourceKind.HANDLER, f) for f in root.rglob("handler*.py")
    ]
    files += [
        (EnumBindingSourceKind.HANDLER, f) for f in (root / "handlers").rglob("*.py")
    ]
    model_roots = [
        root / "models",
        *(REPO / d for d in DATA["model_dirs"].get(node, [])),
    ]
    files += [(EnumBindingSourceKind.MODEL, f) for f in root.glob("models.py")]
    files += [
        (EnumBindingSourceKind.MODEL, f)
        for directory in model_roots
        for f in directory.rglob("*.py")
    ]
    seen: set[Path] = set()
    for kind, file in sorted(files, key=lambda pair: pair[1]):
        if file in seen or file.name == "__init__.py":
            continue
        seen.add(file)
        sources.append(
            ModelBindingSource(
                node=node, path=str(file.relative_to(REPO)), kind=kind, text=_read(file)
            )
        )
    return tuple(sources)


def _real_request(
    checked: list[str], data: dict[str, Any] = DATA
) -> ModelBindingConformanceRequest:
    return ModelBindingConformanceRequest.model_validate(
        {
            "checked_nodes": checked,
            "protocol_table": {"protocols": data["protocols"], "rows": data["rows"]},
            "concrete_bindings": data["concrete_bindings"],
            "pure_forbidden_modules": data["pure_forbidden_modules"],
            "private_names": [
                n.model_dump(mode="json") for n in PRIVATE_NAMES
            ],  # the CI caller's fixture overlay of invented names
            "sources": [s for node in checked for s in _real_sources(node)],
        }
    )


def test_every_listed_node_passes_against_its_real_contract_and_sources() -> None:
    checked = list(DATA["checked_nodes"])
    assert checked, "the checked set is empty"
    result = HandlerBindingConformance().handle(_real_request(checked))
    assert result.issues == ()
    assert result.conforming is True
    assert list(result.checked_nodes) == checked


def test_every_listed_node_has_a_row_and_exists() -> None:
    rows = {r["node"] for r in DATA["rows"]}
    for node in DATA["checked_nodes"]:
        assert node in rows
        assert (NODES / node / "contract.yaml").is_file()
        kinds = {source.kind for source in _real_sources(node)}
        assert EnumBindingSourceKind.HANDLER in kinds, f"{node} has no handler source"


def test_the_checked_set_catches_a_listed_node_that_declares_a_different_set() -> None:
    """A listed effect whose real contract declares no protocol fails, naming each missing one."""
    node = "node_exception_queue_effect"
    result = HandlerBindingConformance().handle(_real_request([node]))
    assert result.conforming is False
    missing = [
        i.detail
        for i in result.issues
        if i.rule is EnumBindingRule.CONTRACT_MISSING_PROTOCOL
    ]
    assert len(missing) == 5


def test_a_listed_pure_node_whose_row_gains_a_protocol_fails_on_its_real_contract() -> (
    None
):
    data = yaml.safe_load(yaml.safe_dump(DATA))
    for row in data["rows"]:
        if row["node"] == NODE:
            row["protocols"] = ["CLK"]
    result = HandlerBindingConformance().handle(_real_request([NODE], data))
    assert _rules(result) == {EnumBindingRule.CONTRACT_MISSING_PROTOCOL}


# --- AC6: configuration --------------------------------------------------------------------


def test_a_fixture_table_that_adds_a_protocol_to_a_node_makes_the_same_contract_fail() -> (
    None
):
    sources = _effect_sources()
    assert _check(("node_fixture_effect",), sources).conforming is True
    wider = ModelBindingProtocolTable.model_validate(
        {
            "protocols": [p.model_dump() for p in TABLE.protocols],
            "rows": [
                {"node": "node_fixture_effect", "protocols": ["BUS", "CLK", "LED"]},
                {"node": "node_fixture_pure_compute", "protocols": []},
            ],
        }
    )
    result = _check(("node_fixture_effect",), sources, table=wider)
    assert result.conforming is False
    assert _rules(result) == {EnumBindingRule.CONTRACT_MISSING_PROTOCOL}
    assert "LED" in result.issues[0].detail


def test_a_fixture_concrete_binding_list_decides_what_counts_as_concrete() -> None:
    handler = (
        CLEAN_EFFECT + "\nfrom other_adapter import OtherAdapter\nOtherAdapter()\n"
    )
    sources = _effect_sources(handler)
    assert _check(("node_fixture_effect",), sources).conforming is True
    request = ModelBindingConformanceRequest(
        checked_nodes=("node_fixture_effect",),
        protocol_table=TABLE,
        concrete_bindings=(
            ModelConcreteBinding(code="BUS", modules=("other_adapter",)),
        ),
        sources=sources,
    )
    assert HandlerBindingConformance().handle(request).conforming is False


def test_the_node_ships_no_protocol_table_binding_name_or_private_name() -> None:
    shipped = [
        *sorted((NODES / NODE).rglob("*.py")),
        *sorted((REPO / "src/omnimarket/models/binding_conformance").rglob("*.py")),
        NODES / NODE / "contract.yaml",
    ]
    text = "\n".join(_read(p) for p in shipped)
    for literal in (
        "aiokafka",
        "confluent_kafka",
        "psycopg",
        "asyncpg",
        "ProtocolEventBus",
        "ProtocolStateStore",
        "node_overlay_reader",
        "contract_loader",
        INVENTED_REPO,
        INVENTED_HOST,
        INVENTED_ORG,
    ):
        assert literal not in text, literal


# --- AC7: bindings -------------------------------------------------------------------------


def test_the_node_declares_no_protocol_and_is_pure() -> None:
    contract = yaml.safe_load(_read(NODES / NODE / "contract.yaml"))
    assert "protocol_dependencies" not in contract
    assert contract["descriptor"]["purity"] == "pure"
    row = next(r for r in DATA["rows"] if r["node"] == NODE)
    assert row["protocols"] == []


def test_the_node_is_registered_and_routes_check_bindings_to_the_handler() -> None:
    pyproject = tomllib.loads(_read(REPO / "pyproject.toml"))
    entries = pyproject["project"]["entry-points"]["onex.nodes"]
    assert entries[NODE] == f"omnimarket.nodes.{NODE}"
    contract = yaml.safe_load(_read(NODES / NODE / "contract.yaml"))
    assert [h["operation"] for h in contract["handler_routing"]["handlers"]] == [
        "check_bindings"
    ]
    route = _resolve_node_route(NODE)
    assert route.command_topic == contract["runtime_dispatch"]["command_topic"]
    assert route.terminal_topic == "onex.evt.omnimarket.binding-conformance-checked.v1"
    handler_class = getattr(
        importlib.import_module(route.handler_module), route.handler_class
    )
    assert handler_class is HandlerBindingConformance


def test_direct_and_contract_routed_results_are_byte_identical_json() -> None:
    """The handler called directly and through the contract's route give the same bytes.

    The parity harness is not in the tree yet; the route is the one `onex run-node` resolves
    from the contract (command topic, input model and handler class), driven in process.
    """
    request = _real_request(list(DATA["checked_nodes"]))
    route = _resolve_node_route(NODE)
    input_model = getattr(
        importlib.import_module(route.input_model_module), route.input_model_name
    )
    routed_handler = getattr(
        importlib.import_module(route.handler_module), route.handler_class
    )()
    routed = routed_handler.handle(
        input_model.model_validate_json(request.model_dump_json())
    )
    direct = HandlerBindingConformance().handle(request)
    assert direct.conforming is True
    assert direct.model_dump_json() == routed.model_dump_json()
    failing = request.model_copy(update={"checked_nodes": ("node_without_a_row",)})
    assert (
        HandlerBindingConformance().handle(failing).model_dump_json()
        == routed_handler.handle(
            input_model.model_validate_json(failing.model_dump_json())
        ).model_dump_json()
    )
    assert HandlerBindingConformance().handle(failing).conforming is False
