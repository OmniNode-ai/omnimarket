# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden facts and golden defect chain of the seam graph scan (OMN-19998).

The loader (``load_seam_graph_facts``) runs over a text-only fixture of two
repo checkouts and one overlay checkout under ``tmp_path`` — no network, no
git. The first test pins the exact facts of every step; the second runs
``HandlerSeamGraphDefects`` end to end and pins every defect group's count.
Two failing controls prove the pins can fail; four error-chain tests pin the
refusals. The last three tests are seams: each fails when either the topic
reader or this node's own contract changes shape under the loader.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
import yaml

from omnimarket.handlers.seam_graph_facts_loader import (
    _COMMAND_SHAPES,
    _EXTERNAL_CONSUMED_SHAPES,
    _EXTERNAL_PRODUCED_SHAPES,
    _PUBLISH_SHAPES,
    _SUBSCRIBE_SHAPES,
    load_seam_graph_facts,
)
from omnimarket.models.model_contract_topics import EnumContractTopicShape
from omnimarket.models.model_seam_graph_defects import (
    EnumSeamDefectClass,
    ModelSeamDefectGroup,
    ModelSeamGraphCounts,
    ModelSeamGraphDefectReport,
    ModelSeamGraphFacts,
    ModelSeamGraphRequest,
    ModelSeamGraphRule,
    ModelSeamNodeFact,
    ModelSeamRepoRule,
)
from omnimarket.nodes.node_seam_graph_defects_compute.handlers.handler_seam_graph_defects import (
    HandlerSeamGraphDefects,
)

pytestmark = pytest.mark.unit

TOPIC_PATTERN = r"^onex\.(evt|cmd)\.[a-z0-9._-]+\.v\d+$"
COMMAND_TOPIC_PATTERN = r"^onex\.cmd\.[a-z0-9._-]+\.v\d+$"
# The repo's broad topic grammar (the one reader exemplar's rule). The scan
# rule above is evt|cmd only, so it can never match a snapshot literal.
ANY_TOPIC_PATTERN = r"^onex\.(evt|cmd|intent|dlq|snapshot)\.[a-z0-9._-]+\.v\d+$"
SYNC_IO_MARKER = r"\bopen\("
LEDGER_MARKER = "FILE_LEDGER_MARKER"
MADE = "onex.evt.a.made.v1"
ORPHAN = "onex.evt.a.orphan.v1"
NODE_CONTRACT_GLOB = "src/*/nodes/*/contract.yaml"
OMNIMARKET_ROOT = Path(__file__).parents[3]
NODE_DIR = (
    OMNIMARKET_ROOT / "src" / "omnimarket" / "nodes" / "node_seam_graph_defects_compute"
)


def _write(path: Path, text: str) -> Path:
    """Writes text under tmp_path, creating parent directories."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _contract(node_dir: Path, document: dict[str, Any]) -> Path:
    """Writes one node's contract.yaml."""
    return _write(node_dir / "contract.yaml", yaml.safe_dump(document))


def _make_rule(
    sync_io_markers: tuple[str, ...] = (SYNC_IO_MARKER,),
) -> ModelSeamGraphRule:
    """The overlay rule of the fixture scan, with overridable sync markers."""
    return ModelSeamGraphRule(
        topic_pattern=TOPIC_PATTERN,
        command_topic_pattern=COMMAND_TOPIC_PATTERN,
        first_n=10,
        repos=(
            ModelSeamRepoRule(
                name="repo_a",
                package="pkg_a",
                source_dir="src",
                contract_glob=NODE_CONTRACT_GLOB,
                caller_scan_dirs=("src",),
                test_dirs=("tests",),
            ),
            ModelSeamRepoRule(
                name="repo_b",
                package="pkg_b",
                source_dir="src",
                contract_glob=NODE_CONTRACT_GLOB,
                caller_scan_dirs=("src",),
                test_dirs=("tests",),
            ),
            ModelSeamRepoRule(
                name="overlays",
                package="overlays",
                source_dir="overlays",
                contract_glob="contracts/*/contract.yaml",
                caller_scan_dirs=("overlays",),
                test_dirs=(),
            ),
        ),
        overlay_repo="overlays",
        overlay_row_glob="overlays/{node}/overlay.yaml",
        scanned_extensions=(".py", ".yaml"),
        excluded_dir_names=("tests",),
        sync_io_markers=sync_io_markers,
        ledger_reader_markers=(LEDGER_MARKER,),
        replay_node_markers=("replay",),
        shared_topic_min_consumers=2,
        catalog_file_node_threshold=4,
    )


def _request(
    tree: dict[str, str], rule: ModelSeamGraphRule | None = None
) -> ModelSeamGraphRequest:
    """A scan request over the fixture tree (or a variant rule)."""
    return ModelSeamGraphRequest(
        rule=rule if rule is not None else _make_rule(), repo_roots=tree
    )


def _node(facts: ModelSeamGraphFacts, name: str) -> ModelSeamNodeFact:
    """The one node fact of the given name."""
    return next(node for node in facts.nodes if node.name == name)


def _group(
    report: ModelSeamGraphDefectReport, defect_class: EnumSeamDefectClass
) -> ModelSeamDefectGroup:
    """The one defect group of the given class."""
    return next(group for group in report.groups if group.defect_class is defect_class)


@pytest.fixture
def tree(tmp_path: Path) -> dict[str, str]:
    """Two repo checkouts and one overlay checkout under tmp_path, all text.

    repo_a owns pkg_a (six nodes, one catalog); repo_b owns pkg_b (node_b)
    and imports two pkg_a modules; the overlays checkout holds node_sub's
    overlay row. The seam-test literals are placed per the loader's rule that
    only the OWNER repo's tests cover an import: repo_b's test_owner.py names
    pkg_b.core.mod exactly (covered), repo_b's test_seam.py holds the exact
    "pkg_a.exposed" literal on the importer side (not covered), and repo_a's
    test_seam.py names only the longer attribute path "pkg_a.exposed.thing"
    (not covered either) — so pkg_a.exposed and pkg_a.hidden arrive uncovered.
    """
    nodes_a = tmp_path / "repo_a" / "src" / "pkg_a" / "nodes"
    _contract(
        nodes_a / "node_pub",
        {
            "name": "node_pub",
            "node_type": "compute",
            "event_bus": {"publish_topics": [MADE], "subscribe_topics": []},
        },
    )
    _contract(
        nodes_a / "node_sub",
        {
            "name": "node_sub",
            "node_type": "effect",
            "event_bus": {"subscribe_topics": [MADE, ORPHAN], "publish_topics": []},
        },
    )
    _write(
        nodes_a / "node_sub" / "handlers" / "handler_sub.py",
        'class H:\n    def handle(self, request):\n        return open("x").read()\n',
    )
    _write(nodes_a / "node_sub" / "overlay.py", "# overlay hooks\n")
    _write(nodes_a / "node_sub" / "overlay.example.yaml", "alpha: 1\nbeta: 2\n")
    _contract(nodes_a / "node_lonely", {"name": "node_lonely", "node_type": "compute"})
    _write(
        nodes_a / "node_lonely" / "handlers" / "handler_lonely.py",
        "class H:\n    async def handle(self, request):\n"
        '        return "FILE_LEDGER_MARKER"\n',
    )
    for name in ("node_c1", "node_c2", "node_c3"):
        _contract(nodes_a / name, {"name": name, "node_type": "compute"})
    _write(
        tmp_path / "repo_a" / "src" / "pkg_a" / "glue.py",
        'import pkg_b.core.mod\n\nCALLS = "node_pub"\n',
    )
    _write(
        tmp_path / "repo_a" / "src" / "pkg_a" / "catalog.yaml",
        yaml.safe_dump(
            {
                "nodes": [
                    "node_pub",
                    "node_sub",
                    "node_lonely",
                    "node_c1",
                    "node_c2",
                    "node_c3",
                ]
            }
        ),
    )
    _write(
        tmp_path / "repo_a" / "tests" / "test_seam.py",
        'LONGER_ATTRIBUTE_PATH = "pkg_a.exposed.thing"\n',
    )

    src_b = tmp_path / "repo_b" / "src" / "pkg_b"
    _write(src_b / "core" / "mod.py", "")
    _write(src_b / "core" / "other.py", "")
    _contract(src_b / "nodes" / "node_b", {"name": "node_b", "node_type": "compute"})
    _write(
        src_b / "uses_a.py",
        "import pkg_a.exposed\nfrom pkg_a.hidden import thing\n",
    )
    _write(
        tmp_path / "repo_b" / "tests" / "test_owner.py", 'MODULE = "pkg_b.core.mod"\n'
    )
    _write(
        tmp_path / "repo_b" / "tests" / "test_seam.py",
        'IMPORTER_SIDE = "pkg_a.exposed"\n',
    )

    # The overlay row names no node, so the reference scan never counts it.
    _write(
        tmp_path / "overlays" / "overlays" / "node_sub" / "overlay.yaml", "alpha: 1\n"
    )
    return {
        "repo_a": str(tmp_path / "repo_a"),
        "repo_b": str(tmp_path / "repo_b"),
        "overlays": str(tmp_path / "overlays"),
    }


def test_loader_golden_facts(tree: dict[str, str]) -> None:
    facts = load_seam_graph_facts(_request(tree))

    assert [node.name for node in facts.nodes] == [
        "node_c1",
        "node_c2",
        "node_c3",
        "node_lonely",
        "node_pub",
        "node_sub",
        "node_b",
    ]

    node_sub = _node(facts, "node_sub")
    assert node_sub.subscribe_topics == (MADE, ORPHAN)
    assert node_sub.publish_topics == ()
    assert node_sub.needs_overlay is True
    assert node_sub.overlay_row_present is True
    assert node_sub.overlay_required_keys == ("alpha", "beta")
    assert node_sub.overlay_row_keys == ("alpha",)
    assert node_sub.sync_io_hits == (
        f"src/pkg_a/nodes/node_sub/handlers/handler_sub.py: {SYNC_IO_MARKER}",
    )
    assert node_sub.ledger_reader_hits == ()
    # The overlay row file names no node, so it is never node_sub's caller.
    assert node_sub.referenced_by == ()

    node_lonely = _node(facts, "node_lonely")
    assert node_lonely.ledger_reader_hits == (
        f"src/pkg_a/nodes/node_lonely/handlers/handler_lonely.py: {LEDGER_MARKER}",
    )
    # An `async def handle` is never a sync-IO suspect.
    assert node_lonely.sync_io_hits == ()
    assert node_lonely.referenced_by == ()

    node_pub = _node(facts, "node_pub")
    assert node_pub.publish_topics == (MADE,)
    # glue.py names it; the catalog (six node names) is skipped as a catalog,
    # and nothing inside node_pub's own directory counts.
    assert node_pub.referenced_by == ("repo_a:src/pkg_a/glue.py",)

    # Six readable contracts in repo_a plus node_b in repo_b.
    assert facts.contracts_read == 7
    assert facts.contracts_unreadable == ()

    imports = {(f.module, f.importer_repo): f for f in facts.imports}
    assert set(imports) == {
        ("pkg_b.core.mod", "repo_a"),
        ("pkg_a.exposed", "repo_b"),
        ("pkg_a.hidden", "repo_b"),
    }
    # Owner repo_b's test names pkg_b.core.mod exactly: covered.
    assert imports[("pkg_b.core.mod", "repo_a")].has_seam_test is True
    assert imports[("pkg_b.core.mod", "repo_a")].importer_path == "src/pkg_a/glue.py"
    # repo_a owns pkg_a, and neither non-counting literal form is its exact
    # module name in repo_a's own tests: the exact literal sits only in
    # importer repo_b's tests and repo_a names a longer attribute path.
    assert imports[("pkg_a.exposed", "repo_b")].has_seam_test is False
    # Nothing anywhere names pkg_a.hidden.
    assert imports[("pkg_a.hidden", "repo_b")].has_seam_test is False


def test_end_to_end_golden_chain(tree: dict[str, str]) -> None:
    report = HandlerSeamGraphDefects().handle(_request(tree))

    assert [group.defect_class for group in report.groups] == list(EnumSeamDefectClass)

    orphan = _group(report, EnumSeamDefectClass.TOPIC_SUBSCRIBED_NO_PUBLISHER)
    assert orphan.count == 1
    assert orphan.first[0].subject == ORPHAN
    assert (
        _group(report, EnumSeamDefectClass.TOPIC_PUBLISHED_NO_SUBSCRIBER).count == 0
    )  # onex.evt.a.made.v1 has node_sub as its subscriber

    no_caller = _group(report, EnumSeamDefectClass.NODE_NO_CALLER)
    assert no_caller.count == 5
    assert {defect.subject for defect in no_caller.first} == {
        "node_lonely",
        "node_c1",
        "node_c2",
        "node_c3",
        "node_b",
    }

    assert _group(report, EnumSeamDefectClass.NODE_OVERLAY_ROW_MISSING).count == 0
    keys_missing = _group(report, EnumSeamDefectClass.NODE_OVERLAY_KEYS_MISSING)
    assert keys_missing.count == 1
    assert keys_missing.first[0].subject == "node_sub"
    assert "beta" in keys_missing.first[0].detail

    uncovered = _group(report, EnumSeamDefectClass.CROSS_REPO_IMPORT_NO_SEAM_TEST)
    assert uncovered.count == 2
    assert {defect.subject for defect in uncovered.first} == {
        "pkg_a.exposed",
        "pkg_a.hidden",
    }

    sync_io = _group(report, EnumSeamDefectClass.SYNC_IO_HANDLER)
    assert sync_io.count == 1
    assert sync_io.first[0].subject == "node_sub"
    ledger = _group(report, EnumSeamDefectClass.FILE_LEDGER_READER)
    assert ledger.count == 1
    assert ledger.first[0].subject == "node_lonely"
    assert _group(report, EnumSeamDefectClass.SHARED_TOPIC_REPLAY).count == 0

    assert report.counts == ModelSeamGraphCounts(
        nodes=7,
        topics=2,
        publish_edges=1,
        subscribe_edges=2,
        overlay_edges=1,
        caller_edges=1,
        import_edges=3,
    )
    assert report.contracts_read == 7
    assert report.contracts_unreadable == ()


def test_failing_control_import_seam_test(tree: dict[str, str]) -> None:
    # The literal in importer repo_b's tests does not cover a pkg_a module.
    _write(
        Path(tree["repo_b"]) / "tests" / "test_importer_side.py",
        'IMPORTED = "pkg_a.hidden"\n',
    )
    facts = load_seam_graph_facts(_request(tree))
    hidden = next(f for f in facts.imports if f.module == "pkg_a.hidden")
    assert hidden.has_seam_test is False

    # repo_a owns pkg_a, so the same literal in repo_a's tests covers it.
    _write(
        Path(tree["repo_a"]) / "tests" / "test_owner_side.py",
        'OWNED = "pkg_a.hidden"\n',
    )
    facts = load_seam_graph_facts(_request(tree))
    hidden = next(f for f in facts.imports if f.module == "pkg_a.hidden")
    assert hidden.has_seam_test is True
    # Untouched elsewhere: pkg_a.exposed still has no exact owner literal.
    exposed = next(f for f in facts.imports if f.module == "pkg_a.exposed")
    assert exposed.has_seam_test is False


def test_failing_control_sync_io(tree: dict[str, str]) -> None:
    handler = Path(tree["repo_a"]) / "src/pkg_a/nodes/node_sub/handlers/handler_sub.py"
    handler.write_text(
        handler.read_text(encoding="utf-8").replace("def handle", "async def handle"),
        encoding="utf-8",
    )

    facts = load_seam_graph_facts(_request(tree))

    assert _node(facts, "node_sub").sync_io_hits == ()


def test_error_chain_unreadable_contract_listed(tree: dict[str, str]) -> None:
    _write(
        Path(tree["repo_a"]) / "src/pkg_a/nodes/node_bad/contract.yaml", "[: not yaml"
    )

    facts = load_seam_graph_facts(_request(tree))

    assert facts.contracts_unreadable == ("repo_a/node_bad",)
    # The scan still returns: every readable contract is still read.
    assert facts.contracts_read == 7
    assert _node(facts, "node_sub").subscribe_topics == (MADE, ORPHAN)
    assert _node(facts, "node_bad").node_type == "unknown"


def test_error_chain_missing_repo_root(tree: dict[str, str]) -> None:
    roots = dict(tree)
    del roots["repo_b"]

    with pytest.raises(ValueError, match="repo_b"):
        load_seam_graph_facts(
            ModelSeamGraphRequest(rule=_make_rule(), repo_roots=roots)
        )


def test_error_chain_not_a_directory(tree: dict[str, str], tmp_path: Path) -> None:
    roots = dict(tree)
    roots["repo_b"] = str(_write(tmp_path / "a_file.txt", "not a directory\n"))

    with pytest.raises(ValueError, match="not a directory"):
        load_seam_graph_facts(
            ModelSeamGraphRequest(rule=_make_rule(), repo_roots=roots)
        )


def test_error_chain_invalid_marker_regex(tree: dict[str, str]) -> None:
    with pytest.raises(ValueError, match="is not a valid regex"):
        load_seam_graph_facts(_request(tree, _make_rule(sync_io_markers=("(",))))


def test_seam_every_topic_shape_is_bucketed_or_declared_unbucketed() -> None:
    bucketed = (
        set(_SUBSCRIBE_SHAPES)
        | set(_PUBLISH_SHAPES)
        | set(_EXTERNAL_CONSUMED_SHAPES)
        | set(_EXTERNAL_PRODUCED_SHAPES)
        | set(_COMMAND_SHAPES)
    )
    declared_unbucketed = {EnumContractTopicShape.TOPICS_MAP}
    # A new shape added to the topic reader fails here until the loader buckets
    # it (or it is named above as deliberately unbucketed).
    assert set(EnumContractTopicShape) == bucketed | declared_unbucketed
    # The loader documents COMMAND as counted in both the subscribe and the
    # command buckets; TOPICS_MAP counts nowhere.
    assert set(_COMMAND_SHAPES) <= set(_SUBSCRIBE_SHAPES)
    assert EnumContractTopicShape.TOPICS_MAP not in bucketed


def test_seam_contract_topics_match_loader() -> None:
    contract = yaml.safe_load((NODE_DIR / "contract.yaml").read_text(encoding="utf-8"))
    command_topic = contract["runtime_dispatch"]["command_topic"]
    terminal_events = contract["runtime_dispatch"]["terminal_events"]

    # What the contract declares for the runtime must be what it declares for
    # the bus, or provisioning and the graph read different topics.
    assert command_topic in contract["event_bus"]["subscribe_topics"]
    assert set(terminal_events.values()) == set(contract["event_bus"]["publish_topics"])
    assert re.fullmatch(ANY_TOPIC_PATTERN, command_topic)
    for topic in terminal_events.values():
        assert re.fullmatch(ANY_TOPIC_PATTERN, topic)
    # The projection's topic is a well-formed versioned topic literal. The
    # scan rule's evt|cmd grammar can never match a snapshot literal, so this
    # seam holds it to the repo's broad topic grammar instead.
    projection_topic = contract["projection_api"]["topic"]
    assert isinstance(projection_topic, str)
    assert projection_topic != ""
    assert re.fullmatch(ANY_TOPIC_PATTERN, projection_topic)


def test_seam_real_node_contract_read_by_the_loader() -> None:
    # One rule repo: the omnimarket checkout itself, holding only this node's
    # contract; the overlay row glob matches nothing.
    rule = _make_rule().model_copy(
        update={
            "repos": (
                ModelSeamRepoRule(
                    name="omnimarket",
                    package="omnimarket",
                    source_dir="src/omnimarket/nodes/node_seam_graph_defects_compute",
                    contract_glob=(
                        "src/omnimarket/nodes/"
                        "node_seam_graph_defects_compute/contract.yaml"
                    ),
                    caller_scan_dirs=(
                        "src/omnimarket/nodes/node_contract_topic_reader_compute",
                    ),
                    test_dirs=(),
                ),
            ),
            "overlay_repo": "omnimarket",
            "overlay_row_glob": "does-not-exist/{node}.yaml",
        }
    )

    facts = load_seam_graph_facts(
        ModelSeamGraphRequest(
            rule=rule, repo_roots={"omnimarket": str(OMNIMARKET_ROOT)}
        )
    )

    assert len(facts.nodes) == 1
    node = facts.nodes[0]
    assert node.name == "node_seam_graph_defects_compute"
    assert node.command_topics == (
        "onex.cmd.omnimarket.seam-graph-defects-requested.v1",
    )
    assert node.subscribe_topics == (
        "onex.cmd.omnimarket.seam-graph-defects-requested.v1",
    )
    assert node.publish_topics == (
        "onex.evt.omnimarket.seam-graph-defects-completed.v1",
        "onex.evt.omnimarket.seam-graph-defects-failed.v1",
    )
    assert facts.contracts_read == 1
