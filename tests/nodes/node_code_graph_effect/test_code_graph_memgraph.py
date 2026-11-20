# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The lookup answers from Memgraph by Cypher and falls back with a typed reason (OMN-26104)."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from omnimarket.models.model_code_graph_lookup import (
    EnumCodeGraphQuestion,
    EnumLookupFallbackReason,
    EnumLookupServedFrom,
    ModelCodeGraphLookupCommand,
    ModelCodeGraphLookupResult,
)
from omnimarket.nodes.node_code_graph_effect.adapters.memgraph_version_store import (
    MemgraphUnreachableError,
    MemgraphVersionStore,
    open_memgraph_store,
    revision_key,
    unreachable_reason,
)
from omnimarket.nodes.node_code_graph_effect.adapters.version_stores import (
    InMemoryVersionStore,
)
from omnimarket.nodes.node_code_graph_effect.handlers.code_graph_lookup import (
    CodeGraphLookup,
)
from omnimarket.nodes.node_code_graph_effect.handlers.code_graph_lookup_routed import (
    MemgraphFirstLookup,
)
from tests.nodes.node_code_graph_effect.lookup_support import (
    BASE,
    HANDLER,
    MODELS,
    REPO,
    ask,
    at,
    version,
)
from tests.nodes.node_code_graph_effect.memgraph_support import FakeBoltDriver

pytestmark = pytest.mark.unit


def _store(driver: FakeBoltDriver, retention: int = 3) -> MemgraphVersionStore:
    ticks = iter(range(1, 1000))
    return MemgraphVersionStore(
        driver, retention_versions=retention, clock_ns=lambda: next(ticks)
    )


def _file_fallback(command: ModelCodeGraphLookupCommand) -> ModelCodeGraphLookupResult:
    memory = InMemoryVersionStore(3)
    memory.put(version("file-commit", BASE))
    return CodeGraphLookup(memory, _NoClock()).handle(command)


class _NoClock:
    def committed_at(self, repo: str, commit: str) -> None:
        return None


def _opener(driver: FakeBoltDriver) -> Callable[[], MemgraphVersionStore]:
    return lambda: _store(driver)


def test_a_version_written_to_memgraph_reads_back_equal() -> None:
    driver = FakeBoltDriver()
    store = _store(driver)
    stored = version("c1", BASE, committed_at=at(1))
    assert store.put(stored) == (True, ())
    assert store.newest(REPO) == stored
    assert store.get(REPO, "c1") == stored


def test_the_write_is_one_revision_label_with_nodes_and_edges() -> None:
    driver = FakeBoltDriver()
    stored = version("c1", BASE)
    _store(driver).put(stored)
    key = revision_key(REPO, "c1")
    assert key == f"{REPO}@c1"
    assert {n["rev"] for n in driver.nodes["CgModule"].values()} == {key}
    assert len(driver.nodes["CgSymbol"]) == len(stored.definitions) > 0
    assert len(driver.edges) == len(stored.imports) > 0
    assert all(
        statement.count("MERGE") or statement.startswith(("MATCH", "CREATE INDEX"))
        for statement in driver.statements
    )
    assert not any(s.lstrip().startswith("CREATE (") for s in driver.statements)


def test_a_second_put_of_the_same_commit_writes_nothing() -> None:
    driver = FakeBoltDriver()
    store = _store(driver)
    store.put(version("c1", BASE))
    before = (len(driver.nodes["CgSymbol"]), len(driver.edges), len(driver.statements))
    assert store.put(version("c1", BASE)) == (False, ())
    assert (len(driver.nodes["CgSymbol"]), len(driver.edges)) == before[:2]
    assert len(driver.statements) == before[2] + 1


def test_a_revision_whose_write_died_is_not_read_and_is_rewritten() -> None:
    driver = FakeBoltDriver()
    store = _store(driver)
    store.put(version("c1", BASE))
    driver.nodes["CgRevision"][revision_key(REPO, "c1")]["complete"] = False
    assert store.newest(REPO) is None
    assert store.put(version("c1", BASE)) == (True, ())
    assert store.newest(REPO) is not None


def test_the_retention_prunes_the_oldest_revision_and_its_nodes() -> None:
    driver = FakeBoltDriver()
    store = _store(driver, retention=2)
    store.put(version("c1", BASE))
    store.put(
        version(
            "c2", {**BASE, "src/example_core/added.py": "def added():\n    return 1\n"}
        )
    )
    written, pruned = store.put(version("c3", BASE))
    assert (written, pruned) == (True, ("c1",))
    assert store.get(REPO, "c1") is None
    assert {n["rev"] for n in driver.nodes["CgModule"].values()} == {
        revision_key(REPO, "c2"),
        revision_key(REPO, "c3"),
    }


def test_the_lookup_answers_importers_definition_and_reach_from_memgraph() -> None:
    driver = FakeBoltDriver()
    _store(driver).put(version("c1", BASE))
    lookup = MemgraphFirstLookup(open_store=_opener(driver), fallback=_file_fallback)
    importers = lookup.handle(ask(EnumCodeGraphQuestion.IMPORTERS, path=MODELS))
    assert importers.status == "found"
    assert [m.path for m in importers.matches] == [HANDLER]
    assert importers.served_from is EnumLookupServedFrom.MEMGRAPH
    assert importers.fallback_reason is None
    assert importers.answered_from == "c1"
    definition = lookup.handle(ask(EnumCodeGraphQuestion.DEFINITION, symbol="Thing"))
    assert [(m.path, m.line) for m in definition.matches] == [
        ("src/example_core/lookalike.py", 1),
        (MODELS, 4),
    ]
    reach = lookup.handle(ask(EnumCodeGraphQuestion.REACH, paths=(MODELS,)))
    assert next(m.path for m in reach.matches) == HANDLER
    assert reach.served_from is EnumLookupServedFrom.MEMGRAPH
    assert driver.closed


def test_the_memgraph_answer_matches_the_file_store_answer() -> None:
    driver = FakeBoltDriver()
    stored = version("c1", BASE)
    _store(driver).put(stored)
    memory = InMemoryVersionStore(3)
    memory.put(stored)
    for command in (
        ask(EnumCodeGraphQuestion.IMPORTERS, path=MODELS),
        ask(EnumCodeGraphQuestion.DEFINITION, symbol="Thing"),
        ask(EnumCodeGraphQuestion.REACH, paths=(MODELS,)),
        ask(EnumCodeGraphQuestion.SAME_SHAPE, symbol="Thing"),
        ask(EnumCodeGraphQuestion.EXEMPLAR, archetype="effect"),
    ):
        from_memgraph = MemgraphFirstLookup(
            open_store=_opener(driver), fallback=_file_fallback
        ).handle(command)
        from_files = CodeGraphLookup(memory, _NoClock()).handle(command)
        assert from_memgraph.matches == from_files.matches, command.question


def test_a_repository_memgraph_holds_no_version_of_is_its_own_not_found() -> None:
    lookup = MemgraphFirstLookup(
        open_store=_opener(FakeBoltDriver()), fallback=_file_fallback
    )
    result = lookup.handle(ask(EnumCodeGraphQuestion.IMPORTERS, path=MODELS))
    assert result.status == "not-found"
    assert result.served_from is EnumLookupServedFrom.MEMGRAPH
    assert result.fallback_reason is None


@pytest.mark.parametrize(
    ("error", "reason"),
    [
        (
            ConnectionRefusedError(111, "refused"),
            EnumLookupFallbackReason.CONNECTION_REFUSED,
        ),
        (TimeoutError("read timed out"), EnumLookupFallbackReason.READ_TIMED_OUT),
        (OSError(113, "no route to host"), EnumLookupFallbackReason.UNREACHABLE),
    ],
)
def test_an_unreachable_memgraph_falls_back_with_a_typed_reason(
    error: BaseException, reason: EnumLookupFallbackReason
) -> None:
    lookup = MemgraphFirstLookup(
        open_store=_opener(FakeBoltDriver(fail=error)), fallback=_file_fallback
    )
    result = lookup.handle(ask(EnumCodeGraphQuestion.IMPORTERS, path=MODELS))
    assert result.served_from is EnumLookupServedFrom.FILE_STORE
    assert result.fallback_reason is reason
    assert isinstance(result.fallback_reason, EnumLookupFallbackReason)
    assert result.fallback_detail
    assert result.answered_from == "file-commit"


def test_a_wrapped_refusal_is_still_a_refusal() -> None:
    from neo4j.exceptions import ServiceUnavailable

    with pytest.raises(ServiceUnavailable) as raised:
        raise ServiceUnavailable("cannot connect") from ConnectionRefusedError(111, "x")
    assert (
        unreachable_reason(raised.value) is EnumLookupFallbackReason.CONNECTION_REFUSED
    )
    assert unreachable_reason(ValueError("syntax")) is None


def test_an_unresolved_bolt_uri_falls_back_with_its_reason() -> None:
    def unresolved() -> str:
        raise ValueError("ARCH_GRAPH_BOLT_URI is not bound by the active overlay")

    lookup = MemgraphFirstLookup(
        open_store=lambda: open_memgraph_store(resolve_uri=unresolved),
        fallback=_file_fallback,
    )
    result = lookup.handle(ask(EnumCodeGraphQuestion.IMPORTERS, path=MODELS))
    assert result.fallback_reason is EnumLookupFallbackReason.BOLT_URI_UNRESOLVED
    assert result.served_from is EnumLookupServedFrom.FILE_STORE
    with pytest.raises(MemgraphUnreachableError):
        open_memgraph_store(resolve_uri=unresolved)


def test_a_statement_memgraph_refuses_is_a_failure_and_never_a_fallback() -> None:
    lookup = MemgraphFirstLookup(
        open_store=_opener(FakeBoltDriver(fail=ValueError("Cypher syntax error"))),
        fallback=_file_fallback,
    )
    result = lookup.handle(ask(EnumCodeGraphQuestion.IMPORTERS, path=MODELS))
    assert result.status == "failed"
    assert result.served_from is EnumLookupServedFrom.MEMGRAPH
    assert result.fallback_reason is None


def test_the_nightly_writes_one_revision_per_repository_into_memgraph() -> None:
    from omnimarket.models.model_code_graph import ModelCodeGraphCommand
    from omnimarket.nodes.node_code_graph_effect.handlers.code_graph_run import (
        CodeGraphRun,
    )
    from tests.nodes.node_code_graph_effect.code_graph_support import (
        HOST,
        RUN_AT,
        overlay,
        source,
    )

    driver = FakeBoltDriver()
    handler = CodeGraphRun(
        overlay=overlay(),
        host=HOST,
        source=source("memory", Path()),
        appender=None,
        clock=lambda: RUN_AT,
        version_writer=_store(driver),
    )
    result = handler.handle(
        ModelCodeGraphCommand(correlation_id="nightly-1", write=False)
    )
    assert result.memgraph == "not-configured"
    assert not driver.nodes["CgRevision"]

    from omnimarket.nodes.node_reading_writer_effect.adapters.ledger_adapters import (
        InMemoryLedger,
    )

    writing = CodeGraphRun(
        overlay=overlay(),
        host=HOST,
        source=source("memory", Path()),
        appender=InMemoryLedger(),
        clock=lambda: RUN_AT,
        version_writer=_store(driver),
    )
    first = writing.handle(ModelCodeGraphCommand(correlation_id="nightly-2"))
    assert first.memgraph == "written:2-already-written:0"
    assert {r["repo"] for r in driver.nodes["CgRevision"].values()} == {
        "example_core",
        "example_app",
    }
    assert all(r["complete"] for r in driver.nodes["CgRevision"].values())
    again = writing.handle(ModelCodeGraphCommand(correlation_id="nightly-3"))
    assert again.memgraph == "written:0-already-written:2"


def test_an_unreachable_memgraph_never_fails_the_nightly_reading() -> None:
    from omnimarket.models.model_code_graph import ModelCodeGraphCommand
    from omnimarket.nodes.node_code_graph_effect.handlers.code_graph_run import (
        CodeGraphRun,
    )
    from omnimarket.nodes.node_reading_writer_effect.adapters.ledger_adapters import (
        InMemoryLedger,
    )
    from tests.nodes.node_code_graph_effect.code_graph_support import (
        HOST,
        RUN_AT,
        overlay,
        source,
    )

    writing = CodeGraphRun(
        overlay=overlay(),
        host=HOST,
        source=source("memory", Path()),
        appender=InMemoryLedger(),
        clock=lambda: RUN_AT,
        version_writer=_store(FakeBoltDriver(fail=ConnectionRefusedError(111, "no"))),
    )
    result = writing.handle(ModelCodeGraphCommand(correlation_id="nightly-4"))
    assert result.status == "completed"
    assert result.memgraph == "failed:connection-refused"
