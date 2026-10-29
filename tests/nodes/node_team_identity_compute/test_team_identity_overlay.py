# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The team identity declaration: its loader, validator and role resolver, on fixtures only (OMN-21382)."""

from __future__ import annotations

import hashlib
import os
import shutil
from pathlib import Path

import pytest

from omnimarket.handlers.node_overlay_reader import OVERLAY_ROOTS_ENV
from omnimarket.nodes.node_team_identity_compute.handlers import (
    HandlerTeamIdentity,
    TeamIdentityError,
    check_cross_references,
    load_team_identity,
    resolve_role,
)
from omnimarket.nodes.node_team_identity_compute.models import (
    ModelIdentityClaim,
    ModelTeamIdentityRequest,
)

pytestmark = pytest.mark.unit

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "team_identity"
VALID = FIXTURES / "valid.yaml"

# Each malformed fixture, and the phrase its refusal must carry.
MALFORMED = {
    "no_operator": "exactly one entry must carry role operator; entries []",
    "second_operator": "exactly one entry must carry role operator; entries [0, 1]",
    "missing_field": "entries.1.github_login: missing",
    "blank_field": "entries.1.slack_member_id: string_too_short",
    "duplicate_id": "id repeats across entries [1, 2]",
    "duplicate_slack_member_id": "slack_member_id repeats across entries [1, 2]",
    "duplicate_ledger_principal": "ledger_principals repeat across entries [1, 2]",
    "empty_ledger_principals": "entries.1.ledger_principals: too_short",
    "unknown_entry_field": "entries.1.display_name: extra_forbidden",
    "unknown_top_level_field": "notes: extra_forbidden",
    "wrong_role": "entries.1.role: literal_error",
    "wrong_schema_version": "schema_version: literal_error",
    "missing_schema_version": "schema_version: missing",
    "no_entries": "entries: too_short",
    "reports_to_unknown": "entries.2.reports_to names no entry",
    "reports_to_self": "entries.2.reports_to names its own entry",
}


def _root(base: Path, fixture: Path) -> str:
    """A root holding ``fixture`` as its team identity overlay."""
    target = base / "team_identity" / "overlay.yaml"
    target.parent.mkdir(parents=True)
    shutil.copyfile(fixture, target)
    return str(base)


def _claim(source: str, value: str) -> ModelIdentityClaim:
    return ModelIdentityClaim.model_validate({"source": source, "value": value})


def test_valid_fixture_loads_one_operator_and_members(tmp_path: Path) -> None:
    resolved = load_team_identity([_root(tmp_path / "a", VALID)])
    overlay = resolved.overlay
    assert overlay.schema_version == 1
    assert overlay.operator.id == "fixture-operator"
    assert [entry.role for entry in overlay.entries] == [
        "operator",
        "member",
        "member",
    ]
    assert overlay.entries[2].reports_to == "fixture-member-a"
    assert overlay.entries[2].ledger_principals == (
        "fixture-member-b-dev",
        "fixture-member-b-laptop",
    )
    assert resolved.source.path == str(
        tmp_path / "a" / "team_identity" / "overlay.yaml"
    )
    assert resolved.source.sha256 == hashlib.sha256(VALID.read_bytes()).hexdigest()
    assert resolved.source.schema_version == 1


def test_loader_resolves_through_overlay_roots_env(tmp_path: Path) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    roots = os.pathsep.join([str(empty), _root(tmp_path / "a", VALID)])
    resolved = load_team_identity(environ={OVERLAY_ROOTS_ENV: roots})
    assert resolved.source.path.endswith(
        os.path.join("a", "team_identity", "overlay.yaml")
    )


@pytest.mark.parametrize("name", sorted(MALFORMED))
def test_each_malformed_fixture_is_refused(tmp_path: Path, name: str) -> None:
    fixture = FIXTURES / "malformed" / f"{name}.yaml"
    with pytest.raises(TeamIdentityError) as refusal:
        load_team_identity([_root(tmp_path / "a", fixture)])
    message = str(refusal.value)
    assert MALFORMED[name] in message
    assert "is invalid for team_identity" in message


def test_every_malformed_fixture_is_covered() -> None:
    on_disk = {path.stem for path in (FIXTURES / "malformed").glob("*.yaml")}
    assert on_disk == set(MALFORMED)


def test_refusal_names_no_values(tmp_path: Path) -> None:
    with pytest.raises(TeamIdentityError) as refusal:
        load_team_identity(
            [
                _root(
                    tmp_path / "a",
                    FIXTURES / "malformed" / "duplicate_slack_member_id.yaml",
                )
            ]
        )
    assert "UFAKE" not in str(refusal.value)
    assert "fixture-" not in str(refusal.value)


def test_no_overlay_is_refused_naming_how_to_supply_it(tmp_path: Path) -> None:
    with pytest.raises(TeamIdentityError, match=OVERLAY_ROOTS_ENV):
        load_team_identity([str(tmp_path)])
    with pytest.raises(TeamIdentityError, match="not configured"):
        load_team_identity(environ={})


def test_conflicting_overlay_sources_are_refused(tmp_path: Path) -> None:
    first = _root(tmp_path / "a", VALID)
    second = _root(tmp_path / "b", FIXTURES / "malformed" / "reports_to_self.yaml")
    with pytest.raises(TeamIdentityError, match="conflicting team_identity overlays"):
        load_team_identity([first, second])


def test_identical_overlay_copies_resolve_to_the_first(tmp_path: Path) -> None:
    first = _root(tmp_path / "a", VALID)
    second = _root(tmp_path / "b", VALID)
    resolved = load_team_identity([first, second])
    assert resolved.source.path.startswith(first)


def test_unparseable_overlay_is_refused(tmp_path: Path) -> None:
    broken = tmp_path / "broken.yaml"
    broken.write_text("entries: [unclosed\n", encoding="utf-8")
    with pytest.raises(TeamIdentityError, match="unreadable"):
        load_team_identity([_root(tmp_path / "a", broken)])


def test_cross_references(tmp_path: Path) -> None:
    overlay = load_team_identity([_root(tmp_path / "a", VALID)]).overlay
    developers = {
        "fixture-member-a-dev",
        "fixture-member-b-dev",
        "fixture-member-b-laptop",
    }
    check_cross_references(
        overlay, developer_principals=developers, lab_host_logins={"fixture-lab-login"}
    )
    with pytest.raises(
        TeamIdentityError, match=r"entries\.2\.ledger_principals.*developer"
    ):
        check_cross_references(
            overlay, developer_principals=developers - {"fixture-member-b-laptop"}
        )
    with pytest.raises(
        TeamIdentityError, match=r"entries\.0\.ledger_principals.*lab-host"
    ):
        check_cross_references(overlay, lab_host_logins={"fixture-other-login"})


def test_authenticated_operator_claims_resolve_to_operator(tmp_path: Path) -> None:
    resolved = load_team_identity([_root(tmp_path / "a", VALID)])
    result = resolve_role(
        resolved,
        [
            _claim("slack_auth", "UFAKEOPER01"),
            _claim("linear_auth", "00000000-0000-4000-8000-00000000f001"),
            _claim("broker_binding", "fixture-lab-login"),
        ],
    )
    assert result.privileges == "operator"
    assert result.entry_id == "fixture-operator"
    assert result.role == "operator"
    assert result.diagnostic is None
    assert result.source == resolved.source


def test_member_claim_resolves_to_member_with_team_privileges(tmp_path: Path) -> None:
    resolved = load_team_identity([_root(tmp_path / "a", VALID)])
    result = resolve_role(resolved, [_claim("github_auth", "fixture-member-b-gh")])
    assert (result.privileges, result.entry_id, result.role) == (
        "team",
        "fixture-member-b",
        "member",
    )
    assert result.diagnostic is None


@pytest.mark.parametrize(
    ("claims", "diagnostic"),
    [
        ([], "no identity is available"),
        ([("environment", "fixture-lab-login")], "not positive identification"),
        (
            [("slack_auth", "UFAKEUNKNOWN")],
            "slack_auth identity names no team_identity entry",
        ),
        (
            [("slack_auth", "UFAKEOPER01"), ("github_auth", "fixture-member-a-gh")],
            "name 2 different team_identity entries",
        ),
        (
            [
                ("slack_auth", "UFAKEOPER01"),
                ("broker_binding", "fixture-unknown-login"),
            ],
            "broker_binding identity names no team_identity entry",
        ),
    ],
)
def test_unknown_or_mismatched_identity_gets_team_restrictions(
    tmp_path: Path, claims: list[tuple[str, str]], diagnostic: str
) -> None:
    resolved = load_team_identity([_root(tmp_path / "a", VALID)])
    result = resolve_role(resolved, [_claim(*claim) for claim in claims])
    assert result.privileges == "team"
    assert result.entry_id is None
    assert result.diagnostic is not None
    assert diagnostic in result.diagnostic
    assert "team restrictions apply" in result.diagnostic


def test_handler_never_fails_open_on_a_bad_declaration(tmp_path: Path) -> None:
    operator = (
        _claim("slack_auth", "UFAKEOPER01"),
        _claim("broker_binding", "fixture-lab-login"),
    )
    handler = HandlerTeamIdentity()
    good = handler.handle(
        ModelTeamIdentityRequest(
            claims=operator, overlay_roots=(_root(tmp_path / "a", VALID),)
        )
    )
    assert good.privileges == "operator"
    assert good.source is not None
    malformed = handler.handle(
        ModelTeamIdentityRequest(
            claims=operator,
            overlay_roots=(
                _root(tmp_path / "b", FIXTURES / "malformed" / "second_operator.yaml"),
            ),
        )
    )
    assert malformed.privileges == "team"
    assert malformed.diagnostic is not None
    assert "team restrictions apply" in malformed.diagnostic
    absent = handler.handle(
        ModelTeamIdentityRequest(
            claims=operator, overlay_roots=(str(tmp_path / "none"),)
        )
    )
    assert absent.privileges == "team"
    assert absent.source is None
    unlisted = handler.handle(
        ModelTeamIdentityRequest(
            claims=operator,
            overlay_roots=(str(tmp_path / "a"),),
            lab_host_logins=frozenset({"fixture-other-login"}),
        )
    )
    assert unlisted.privileges == "team"
    assert unlisted.source == good.source


def test_broker_binding_claim_carries_only_the_bound_username(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from omnimarket.delegated_test_loop import lane_bus
    from omnimarket.nodes.node_team_identity_compute.handlers import (
        broker_binding_claim,
    )

    monkeypatch.setattr(lane_bus, "bound_lane_principal", lambda: "fixture-lab-login")
    assert broker_binding_claim() == _claim("broker_binding", "fixture-lab-login")
