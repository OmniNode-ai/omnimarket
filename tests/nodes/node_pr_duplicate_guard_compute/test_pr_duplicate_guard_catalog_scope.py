# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A shared catalog line is excused only in the paths and repository that declare it (OMN-24664).

A pin manifest deletes its own ``sha256:`` lines; two pull requests that delete the same twenty of them are one
change, whatever each adds. A rendered catalog rewrites one such line in every pull request that touches it. The
deployment overlay declares the second case per repository, as a path glob and a line pattern beside
``generated_paths``; nothing is excused anywhere it does not name, and a pattern that does not compile refuses at load.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from omnimarket.models.model_pr_duplicate_guard import (
    EnumDuplicateEvidence,
    ModelDuplicateDigestConfig,
    ModelOpenPr,
    ModelPrDuplicateGuardRequest,
    ModelPrDuplicateGuardResult,
    ModelPrTouch,
)
from omnimarket.models.node_overlays.model_node_overlays import ModelSharedCatalogLine
from omnimarket.nodes.node_pr_duplicate_guard_compute.handlers.handler_pr_duplicate_guard import (
    HandlerPrDuplicateGuard,
)
from omnimarket.nodes.node_pr_duplicate_snapshot_effect.handlers import (
    handler_pr_duplicate_snapshot as snapshot,
)
from omnimarket.nodes.node_pr_duplicate_snapshot_effect.handlers.handler_pr_duplicate_snapshot import (
    DuplicateGuardUncheckedError,
    load_digest_config,
    parse_diff,
)

pytestmark = pytest.mark.unit

NOW = datetime(2026, 11, 16, 1, 30, tzinfo=UTC)
REPO = "OmniNode-ai/omnibase_infra"
MANIFEST = "deploy/checksums.yaml"
CATALOG = "docker/catalog/database-topology/local.yaml"
SHA_LINE = r"^sha256: [0-9a-f]{64}$"


def pins(count: int = 20) -> tuple[str, ...]:
    return tuple(f"sha256: {n:064x}" for n in range(count))


def manifest_diff(added: str, *, path: str = MANIFEST) -> str:
    """``git diff -U0`` of a pin manifest: twenty pins deleted, one line added in their place."""
    return (
        f"diff --git a/{path} b/{path}\n"
        f"--- a/{path}\n"
        f"+++ b/{path}\n"
        "@@ -10,20 +10 @@ pins:\n"
        + "".join(f"-{pin}\n" for pin in pins())
        + f"+# {added}\n"
    )


def declared(*, repo: str = "omnibase_infra", path: str) -> ModelDuplicateDigestConfig:
    return load_digest_config().with_shared_catalog_lines(
        {repo: (ModelSharedCatalogLine(path=path, line=SHA_LINE),)}
    )


def verdict(
    mine: ModelPrTouch, theirs: ModelPrTouch, *, other_ticket: str
) -> ModelPrDuplicateGuardResult:
    other = ModelOpenPr(
        number=6001,
        head_ref="other-change",
        base="dev",
        title=f"{other_ticket}: the other change",
        touch=theirs,
    )
    return HandlerPrDuplicateGuard().handle(
        ModelPrDuplicateGuardRequest.model_validate(
            {
                "now": NOW,
                "repo": REPO,
                "head_ref": "new-change",
                "base": "dev",
                "title": "OMN-1001: the new change",
                "touch": mine,
                "open_prs": (other,),
            }
        )
    )


@pytest.mark.parametrize("other_ticket", ["OMN-1001", "OMN-1002"])
def test_duplicate_guard_catalog_path_scope_pin_manifest_shared_deletions_refuse(
    other_ticket: str,
) -> None:
    """The reviewed probe: the same 20 sha256 lines of deploy/checksums.yaml deleted, one different line added each."""
    mine, theirs = manifest_diff("first change"), manifest_diff("second change")

    result = verdict(parse_diff(mine), parse_diff(theirs), other_ticket=other_ticket)

    assert result.state == "adopt"
    assert result.adopt_pr == "omnibase_infra#6001"
    assert result.evidence[0].kind is EnumDuplicateEvidence.LINES


@pytest.mark.parametrize("other_ticket", ["OMN-1001", "OMN-1002"])
def test_duplicate_guard_catalog_path_scope_a_declaration_for_another_path_leaves_them_refused(
    other_ticket: str,
) -> None:
    config = declared(path="docker/catalog/*")
    mine, theirs = manifest_diff("first change"), manifest_diff("second change")

    result = verdict(
        parse_diff(mine, repo=REPO, config=config),
        parse_diff(theirs, repo=REPO, config=config),
        other_ticket=other_ticket,
    )

    assert result.state == "adopt"


def test_duplicate_guard_catalog_path_scope_a_declaration_for_another_repository_leaves_them_refused() -> (
    None
):
    config = declared(repo="omnidash", path=MANIFEST)
    mine, theirs = manifest_diff("first change"), manifest_diff("second change")

    result = verdict(
        parse_diff(mine, repo=REPO, config=config),
        parse_diff(theirs, repo=REPO, config=config),
        other_ticket="OMN-1002",
    )

    assert result.state == "adopt"


def test_duplicate_guard_catalog_path_scope_the_declared_path_is_excused_where_it_is_declared() -> (
    None
):
    """Control: the same lines, declared for this very path, are the catalog's rewrite and say nothing."""
    config = declared(path=MANIFEST)
    mine, theirs = manifest_diff("first change"), manifest_diff("second change")

    result = verdict(
        parse_diff(mine, repo=REPO, config=config),
        parse_diff(theirs, repo=REPO, config=config),
        other_ticket="OMN-1002",
    )

    assert result.state == "clear"
    assert result.evidence == ()


def test_duplicate_guard_catalog_path_scope_a_removed_catalog_line_in_another_file_still_counts() -> (
    None
):
    """One diff touching the catalog and a pin manifest excuses the catalog's line only."""
    config = declared(path="docker/catalog/*")
    catalog = (
        f"diff --git a/{CATALOG} b/{CATALOG}\n--- a/{CATALOG}\n+++ b/{CATALOG}\n"
        f"@@ -493 +493 @@ source:\n-{pins(1)[0]}\n+sha256: {'f' * 64}\n"
    )
    touch = parse_diff(
        catalog + manifest_diff("first change"), repo=REPO, config=config
    )
    by_path = {f.path: f for f in touch.files}
    plain = {f.path: f for f in parse_diff(manifest_diff("first change")).files}

    assert by_path[MANIFEST].digest == plain[MANIFEST].digest
    assert len(by_path[MANIFEST].digest) == 21
    assert len(by_path[CATALOG].digest) == 1, "only the added fingerprint is scored"


def test_duplicate_guard_catalog_pattern_invalid_is_refused_by_the_model() -> None:
    with pytest.raises(ValidationError, match="does not compile"):
        ModelSharedCatalogLine(path="docker/catalog/*", line="sha256: [0-9a-f")
    with pytest.raises(ValidationError, match="does not compile"):
        ModelDuplicateDigestConfig.model_validate(
            {
                "min_line_chars": 4,
                "prefix_chars": 12,
                "shared_catalog_lines": {
                    "omnibase_infra": [{"path": "docker/*", "line": "(unclosed"}]
                },
            }
        )


def overlay_root(tmp_path: Path, text: str) -> Path:
    node = tmp_path / "node_pr_duplicate_guard_compute"
    node.mkdir()
    (node / "overlay.yaml").write_text(text, encoding="utf-8")
    return tmp_path


def test_duplicate_guard_catalog_pattern_invalid_in_the_overlay_checks_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = overlay_root(
        tmp_path,
        "generated_paths:\n  omnibase_infra:\n    - plans/INDEX.md\n"
        "shared_catalog_lines:\n  omnibase_infra:\n    - path: docker/catalog/*\n      line: 'sha256: [0-9a-f'\n",
    )
    monkeypatch.setenv(snapshot.OVERLAY_ROOTS_ENV, str(root))

    with pytest.raises(DuplicateGuardUncheckedError, match="deployment overlay"):
        snapshot._load_overlay()


def test_duplicate_guard_catalog_pattern_valid_overlay_declares_path_and_line_per_repository(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = overlay_root(
        tmp_path,
        "generated_paths:\n  omnibase_infra:\n    - plans/INDEX.md\n"
        "shared_catalog_lines:\n  omnibase_infra:\n    - path: docker/catalog/*\n"
        f"      line: '{SHA_LINE}'\n",
    )
    monkeypatch.setenv(snapshot.OVERLAY_ROOTS_ENV, str(root))

    overlay = snapshot._load_overlay()

    assert overlay is not None
    config = load_digest_config().with_shared_catalog_lines(
        overlay.shared_catalog_lines
    )
    assert config.is_shared_catalog_line(REPO, CATALOG, pins(1)[0])
    assert not config.is_shared_catalog_line(REPO, MANIFEST, pins(1)[0])
    assert not config.is_shared_catalog_line("omnidash", CATALOG, pins(1)[0])


ROOT = Path(__file__).resolve().parents[3]
RULE_FILES = (
    "src/omnimarket/nodes/node_pr_duplicate_guard_compute/contract.yaml",
    "src/omnimarket/nodes/node_pr_duplicate_guard_compute/handlers/handler_pr_duplicate_guard.py",
    "src/omnimarket/models/model_pr_duplicate_guard.py",
    "contracts/OMN-19998.yaml",
    "contracts/OMN-24632.yaml",
    "contracts/OMN-24648.yaml",
    "tests/nodes/node_pr_duplicate_guard_compute/test_pr_duplicate_guard_shared_base_line.py",
    "tests/nodes/node_pr_duplicate_guard_compute/test_pr_duplicate_guard_shared_replaced_line.py",
)
#: Spelled in halves so this file never carries the wording it forbids.
STALE = (
    "a fifth",
    "added lines or their removed lines",
    "added_digest",
    "removed_digest",
    "shared_catalog_line_patterns",
)


@pytest.mark.parametrize("name", RULE_FILES)
def test_duplicate_guard_share_rule_stated_once(name: str) -> None:
    """Every file that states the share rule states the one rule, and none keeps a rule the guard no longer has."""
    text = " ".join((ROOT / name).read_text(encoding="utf-8").split())

    assert "one share over all the changed lines" in text, name
    assert "shared catalog lines" in text, name
    assert [stale for stale in STALE if stale in text] == [], name


@pytest.mark.parametrize(
    "name", ["contracts/OMN-24632.yaml", "contracts/OMN-24648.yaml"]
)
def test_duplicate_guard_contracts_declare_the_additive_interface_change(
    name: str,
) -> None:
    text = (ROOT / name).read_text(encoding="utf-8")

    assert "interface_change: false" not in text
    assert "interface_change: true" in text


def test_duplicate_guard_additive_interface_omn24648_ac4_is_bound_to_the_finding_tests() -> (
    None
):
    """AC4 ('every other finding fixed or argued') is bound to the tests of those findings, not to a bare suite pass."""
    contract = yaml.safe_load(
        (ROOT / "contracts/OMN-24648.yaml").read_text(encoding="utf-8")
    )
    bound = [e for e in contract["dod_evidence"] if "AC4" in e.get("binds_ac", [])]

    assert len(bound) == 1
    command = bound[0]["checks"][0]["check_value"]
    for finding in (
        "catalog_path_scope",
        "catalog_pattern",
        "shared_line_among_distinct",
    ):
        assert finding in command
