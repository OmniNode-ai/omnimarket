# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The ``users`` question: who still uses a removed name, answered from the imports as written (OMN-26197).

Each case builds a fixture repository and asks ``users`` for one identity. The answer is matched
against what each file wrote, never resolved against a head, so an import of a name that no longer
exists is still found.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest
from pydantic import ValidationError

from omnimarket.models.model_code_graph import ModelRepoSnapshot
from omnimarket.models.model_code_graph_lookup import (
    EnumCodeGraphQuestion,
    EnumLookupFallbackReason,
    EnumLookupServedFrom,
    EnumReferenceForm,
    EnumRemovalKind,
    ModelCodeGraphLookupCommand,
    ModelCodeGraphLookupResult,
)
from omnimarket.nodes.node_code_graph_effect.adapters.memgraph_version_store import (
    MemgraphVersionStore,
)
from omnimarket.nodes.node_code_graph_effect.adapters.version_stores import (
    InMemoryCommitClock,
    InMemoryVersionStore,
)
from omnimarket.nodes.node_code_graph_effect.handlers.code_graph_lookup import (
    CodeGraphLookup,
)
from omnimarket.nodes.node_code_graph_effect.handlers.code_graph_lookup_routed import (
    MemgraphFirstLookup,
)
from omnimarket.nodes.node_code_graph_effect.handlers.graph_compute import (
    GraphRules,
    build_graph,
)
from tests.nodes.node_code_graph_effect.lookup_support import (
    CORRELATION,
    REPO,
    rules,
    version,
)

pytestmark = pytest.mark.unit

B = "src/b/consumer.py"
SYMBOL = EnumRemovalKind.PYTHON_SYMBOL
MODULE = EnumRemovalKind.PYTHON_MODULE
DIRECT = EnumReferenceForm.DIRECT_IMPORT
ATTRIBUTE = EnumReferenceForm.ATTRIBUTE_USE
REEXPORT = EnumReferenceForm.RE_EXPORT
STAR = EnumReferenceForm.STAR_IMPORT
LITERAL = EnumReferenceForm.IMPORT_MODULE_LITERAL

#: The first thirty-two lines of omnimarket's ``node_onex_setup_effect/cli.py`` at d43c1454aa, the
#: file the removal of ``run_node_in_process`` left importing a name that no longer existed.
FROZEN_CLI_HEAD = '''# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Select and review one typed setup plan, then run the shared setup engine.

Interactive and unattended commands use the same versioned state and adapters.
Verification observes without provisioning. Credentials enter only through hidden
input or stdin and are never included in a saved plan.
"""

from __future__ import annotations

import sys
from collections.abc import Callable
from functools import wraps
from getpass import getpass
from importlib import resources
from pathlib import Path
from shutil import get_terminal_size
from typing import Any

import click
import yaml
from click.core import ParameterSource
from omnibase_core.cli.cli_run_node import run_node_in_process
from omnibase_core.cli.cli_user_config import user_config_path
from omnibase_core.errors.model_onex_error import ModelOnexError
from pydantic import SecretStr

from omnimarket.handlers.handler_endpoint_catalog import HandlerEndpointCatalog
from omnimarket.handlers.handler_endpoint_probe import HandlerModelProbe
from omnimarket.handlers.routing import byok_provider_backends
'''


@dataclass(frozen=True)
class Case:
    files: dict[str, str]
    kind: EnumRemovalKind
    identity: str
    #: ``(path, line, reference form)`` of each user, sorted.
    expected: list[tuple[str, int, EnumReferenceForm]]
    star_export_list: tuple[str, ...] | None = None
    head_all_lists_name: bool = False
    rebound_submodules: tuple[str, ...] = field(default_factory=tuple)


def users(case: Case) -> list[tuple[str, int, EnumReferenceForm | None]]:
    store = InMemoryVersionStore(retention_versions=2)
    store.put(version("c1", case.files))
    result = CodeGraphLookup(store, InMemoryCommitClock({})).handle(
        ModelCodeGraphLookupCommand(
            correlation_id=CORRELATION,
            question=EnumCodeGraphQuestion.USERS,
            repo=REPO,
            removal_kind=case.kind,
            identity=case.identity,
            star_export_list=case.star_export_list,
            head_all_lists_name=case.head_all_lists_name,
            rebound_submodules=case.rebound_submodules,
        )
    )
    return [(m.path, m.line or 0, m.reference_form) for m in result.matches]


def one(
    source: str,
    kind: EnumRemovalKind,
    identity: str,
    expected: list[tuple[str, int, EnumReferenceForm]],
    *,
    extra: dict[str, str] | None = None,
    **options: Any,
) -> Case:
    return Case({B: source, **(extra or {})}, kind, identity, expected, **options)


CASES: dict[str, Case] = {
    # AC1: a dangling import of a name the provider no longer defines.
    "dangling-from-import": one(
        "from a.mod import f\n\n\ndef run():\n    return f()\n",
        SYMBOL,
        "a.mod.f",
        [(B, 1, DIRECT)],
        extra={"src/a/mod.py": "def other():\n    return 1\n"},
    ),
    "dangling-relative-import-in-package-a": Case(
        {
            "src/a/__init__.py": "",
            "src/a/user.py": "from .mod import f\n",
            "src/a/mod.py": "def other():\n    return 1\n",
        },
        SYMBOL,
        "a.mod.f",
        [("src/a/user.py", 1, DIRECT)],
    ),
    "frozen-cli-head-of-omnimarket-d43c1454aa": Case(
        {"src/omnimarket/nodes/node_onex_setup_effect/cli.py": FROZEN_CLI_HEAD},
        SYMBOL,
        "omnibase_core.cli.cli_run_node.run_node_in_process",
        [("src/omnimarket/nodes/node_onex_setup_effect/cli.py", 24, DIRECT)],
    ),
    # AC2: reference forms, in pairs.
    "plain-import-without-use-is-not-a-user": one(
        "import a.mod\n\nVALUE = a.mod\n", SYMBOL, "a.mod.f", []
    ),
    "plain-import-with-attribute-use-is-a-user": one(
        "import a.mod\n\n\ndef run():\n    return a.mod.f()\n",
        SYMBOL,
        "a.mod.f",
        [(B, 5, ATTRIBUTE)],
    ),
    "aliased-import-with-attribute-use": one(
        "import a.mod as m\n\nVALUE = m.f\n", SYMBOL, "a.mod.f", [(B, 3, ATTRIBUTE)]
    ),
    "from-import-under-type-checking": one(
        "from typing import TYPE_CHECKING\n\nif TYPE_CHECKING:\n    from a.mod import f\n",
        SYMBOL,
        "a.mod.f",
        [(B, 4, DIRECT)],
    ),
    "re-export-by-a-package-init": Case(
        {"src/a/__init__.py": "from .mod import f\n"},
        SYMBOL,
        "a.mod.f",
        [("src/a/__init__.py", 1, REEXPORT)],
    ),
    "re-export-by-a-redundant-alias": one(
        "from a.mod import f as f\n", SYMBOL, "a.mod.f", [(B, 1, REEXPORT)]
    ),
    "consumer-of-a-package-reexport-matches-the-package-identity": one(
        "from a import f\n", SYMBOL, "a.f", [(B, 1, DIRECT)]
    ),
    "star-import-of-a-listed-name-the-file-uses": one(
        "from a.mod import *\n\nVALUE = f()\n",
        SYMBOL,
        "a.mod.f",
        [(B, 1, STAR)],
        star_export_list=("f", "g"),
    ),
    "star-import-of-a-listed-name-the-file-never-uses": one(
        "from a.mod import *\n\nVALUE = g()\n",
        SYMBOL,
        "a.mod.f",
        [],
        star_export_list=("f", "g"),
    ),
    "star-import-of-a-name-the-file-binds-itself": one(
        "from a.mod import *\n\n\ndef run():\n    f = 1\n    return f\n",
        SYMBOL,
        "a.mod.f",
        [],
        star_export_list=("f",),
    ),
    "star-import-binding-in-an-unrelated-scope-hides-nothing": one(
        "from a.mod import *\n\n\ndef other(f):\n    return f\n\n\nVALUE = f()\n",
        SYMBOL,
        "a.mod.f",
        [(B, 1, STAR)],
        star_export_list=("f",),
    ),
    "star-import-of-a-name-the-list-leaves-out": one(
        "from a.mod import *\n\nVALUE = f()\n",
        SYMBOL,
        "a.mod.f",
        [],
        star_export_list=("g",),
    ),
    "star-import-with-the-head-all-still-listing-the-name": one(
        "from a.mod import *\n",
        SYMBOL,
        "a.mod.f",
        [(B, 1, STAR)],
        star_export_list=("f",),
        head_all_lists_name=True,
    ),
    "star-import-with-no-list-matches-nothing": one(
        "from a.mod import *\n\nVALUE = f()\n",
        SYMBOL,
        "a.mod.f",
        [],
        head_all_lists_name=True,
    ),
    "from-import-is-a-user-of-the-removed-module": one(
        "from a.mod import f\n", MODULE, "a.mod", [(B, 1, DIRECT)]
    ),
    "rebound-alias-is-not-a-use": one(
        "import a.mod as m\n\nm = other\nVALUE = m.f\n", SYMBOL, "a.mod.f", []
    ),
    "import-module-literal-without-use-of-the-name": one(
        "from importlib import import_module\n\nm = import_module('a.mod')\n",
        SYMBOL,
        "a.mod.f",
        [],
    ),
    "import-module-literal-with-use-of-the-name": one(
        "from importlib import import_module\n\nm = import_module('a.mod')\nVALUE = m.f\n",
        SYMBOL,
        "a.mod.f",
        [(B, 4, ATTRIBUTE)],
    ),
    "import-module-literal-through-importlib-is-a-module-user": one(
        "import importlib\n\nm = importlib.import_module('a.mod')\n",
        MODULE,
        "a.mod",
        [(B, 3, LITERAL)],
    ),
    "plain-import-is-a-user-of-the-removed-module": one(
        "import a.mod\n", MODULE, "a.mod", [(B, 1, DIRECT)]
    ),
    "plain-import-is-not-a-user-of-a-symbol-named-like-the-module": one(
        "import a.mod\n", SYMBOL, "a.mod", []
    ),
    "from-package-import-submodule-then-use": one(
        "from a import mod\n\nVALUE = mod.f()\n", SYMBOL, "a.mod.f", [(B, 3, ATTRIBUTE)]
    ),
    "from-package-import-submodule-the-package-rebinds": one(
        "from a import mod\n\nVALUE = mod.f()\n",
        SYMBOL,
        "a.mod.f",
        [],
        rebound_submodules=("a.mod",),
    ),
    "from-package-import-submodule-is-a-user-of-the-module": one(
        "from a import mod\n", MODULE, "a.mod", [(B, 1, DIRECT)]
    ),
    "from-package-import-rebound-name-is-not-a-user-of-the-module": one(
        "from a import mod\n", MODULE, "a.mod", [], rebound_submodules=("a.mod",)
    ),
    "local-function-named-import-module-is-not-importlib": one(
        "def import_module(name):\n    return name\n\n\nm = import_module('a.mod')\nVALUE = m.f\n",
        SYMBOL,
        "a.mod.f",
        [],
    ),
    "local-function-named-import-module-is-not-a-module-user": one(
        "def import_module(name):\n    return name\n\n\nm = import_module('a.mod')\n",
        MODULE,
        "a.mod",
        [],
    ),
}


@pytest.mark.parametrize("name", sorted(CASES))
def test_users_case(name: str) -> None:
    case = CASES[name]
    assert users(case) == case.expected, name


def _command(**options: Any) -> ModelCodeGraphLookupCommand:
    return ModelCodeGraphLookupCommand(
        correlation_id=CORRELATION,
        question=EnumCodeGraphQuestion.USERS,
        repo=REPO,
        **options,
    )


def test_a_users_question_needs_its_kind_and_identity() -> None:
    with pytest.raises(ValidationError):
        _command(identity="a.mod.f")
    with pytest.raises(ValidationError):
        _command(removal_kind=SYMBOL)


def test_users_is_answered_from_the_file_store_never_from_memgraph() -> None:
    """Memgraph holds no written imports, so its answer would be a clean negative."""
    case = CASES["dangling-from-import"]
    store = InMemoryVersionStore(retention_versions=2)
    store.put(version("c1", case.files))

    def fallback(command: ModelCodeGraphLookupCommand) -> ModelCodeGraphLookupResult:
        return CodeGraphLookup(store, InMemoryCommitClock({})).handle(command)

    def refuse() -> MemgraphVersionStore:
        raise AssertionError("the users question must not open Memgraph")

    result = MemgraphFirstLookup(
        open_store=refuse,
        fallback=fallback,
    ).handle(_command(removal_kind=SYMBOL, identity="a.mod.f"))
    assert result.status == "found"
    assert [m.path for m in result.matches] == [B]
    assert result.served_from is EnumLookupServedFrom.FILE_STORE
    assert result.fallback_reason is EnumLookupFallbackReason.QUESTION_NOT_IN_MEMGRAPH


#: Every spelling the graph resolves, with one import that resolves to nothing.
EDGE_WORLD = {
    "src/a/__init__.py": "from .mod import f\n",
    "src/a/mod.py": "def f():\n    return 1\n",
    "src/a/lazy.py": "from typing import TYPE_CHECKING\n\nif TYPE_CHECKING:\n    from a.mod import f\n",
    "src/b/__init__.py": "",
    "src/b/one.py": "import a.mod\nimport a.mod as m\nfrom a import mod\nfrom a.mod import f\nfrom a.mod import *\n",
    "src/b/two.py": "import importlib\nfrom importlib import import_module\n\nx = importlib.import_module('a.mod')\ny = import_module('a.lazy')\nz = __import__('a')\n",
    "src/b/dangling.py": "from a.gone import g\nfrom .missing import h\nimport a.vanished\n",
}


def test_keeping_written_imports_leaves_the_resolved_edges_alone() -> None:
    """The edge set, the roots and what they reach are the same with and without the written imports."""
    from omnimarket.nodes.node_code_graph_effect.adapters.repo_sources import (
        InMemoryRepoSource,
    )
    from tests.nodes.node_code_graph_effect.lookup_support import REF

    graph_rules: GraphRules = rules()
    snapshot: ModelRepoSnapshot = InMemoryRepoSource(
        {REPO: {REF: ("c1", EDGE_WORLD)}}
    ).snapshot(REPO, REF, graph_rules.wanted)
    plain = build_graph([snapshot], graph_rules)
    kept = build_graph([snapshot], graph_rules, keep_written=True)
    assert plain.written == {}
    assert kept.written
    assert kept.edges == plain.edges
    assert kept.reached == plain.reached
    assert kept.roots == plain.roots
    one = ("example_core", "src/b/one.py")
    assert {path for _, path in plain.edges[one]} == {
        "src/a/mod.py",
        "src/a/__init__.py",
    }
    dangling = ("example_core", "src/b/dangling.py")
    #: The three dangling imports reach only the packages they name a prefix of, never a module.
    assert {path for _, path in plain.edges[dangling]} == {
        "src/a/__init__.py",
        "src/b/__init__.py",
    }


def test_an_import_that_resolves_to_no_file_is_still_found() -> None:
    case = Case(EDGE_WORLD, SYMBOL, "a.gone.g", [("src/b/dangling.py", 1, DIRECT)])
    assert users(case) == case.expected
    relative = Case(
        EDGE_WORLD, SYMBOL, "b.missing.h", [("src/b/dangling.py", 2, DIRECT)]
    )
    assert users(relative) == relative.expected


def test_the_sample_judge_gives_the_same_judgment_for_each_of_its_thirty_entries() -> (
    None
):
    from omnimarket.nodes.node_code_graph_effect.adapters.repo_sources import (
        InMemoryRepoSource,
    )
    from omnimarket.nodes.node_code_graph_effect.handlers.graph_compute import (
        judge_sample,
    )
    from tests.nodes.node_code_graph_effect.lookup_support import REF
    from tests.nodes.node_code_graph_effect.test_frozen_sample import (
        REPO as SAMPLE_REPO,
    )
    from tests.nodes.node_code_graph_effect.test_frozen_sample import _frozen, _world

    graph_rules = rules()
    snapshot = InMemoryRepoSource({SAMPLE_REPO: {REF: ("c1", _world())}}).snapshot(
        SAMPLE_REPO, REF, graph_rules.wanted
    )
    sample = _frozen()
    plain = build_graph([snapshot], graph_rules)
    kept = build_graph([snapshot], graph_rules, keep_written=True)
    assert len(sample.entries) == 30
    judgments = [
        (entry.path, (entry.repo, entry.path) in plain.reached)
        for entry in sample.entries
    ]
    assert judgments == [
        (entry.path, (entry.repo, entry.path) in kept.reached)
        for entry in sample.entries
    ]
    assert judge_sample(plain, sample) == judge_sample(kept, sample)
    assert judge_sample(kept, sample).agree == 30
