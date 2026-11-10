# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The lab fill reads the work-state owners from the floor projection bundle (OMN-23427).

When the deployment names a bundle, the owner facts read its ``work_state`` entry instead of the projection file: the
entry is judged against its own bound (the one the floor's reader overlay declares), and its payload is the same
projection the file would carry, so both readers attribute the same owners. A bundle that cannot be read is the error
string in the facts, never an empty dict: an unread owner is not a free one. The seam control writes the same projection
through both readers; the named break renames the entry's payload key ``items`` and the bundle read refuses.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import yaml

from omnimarket.handlers.node_overlay_reader import load_structured_overlay
from omnimarket.models.floor_projection import (
    ModelFloorProjectionBundle,
    ModelFloorProjectionEntry,
)
from omnimarket.models.work_state_projection import (
    ModelTicketSource,
    ModelWorkScope,
    ModelWorkStateRequest,
    ModelWorkStateResult,
    ModelWorkStateRules,
)
from omnimarket.nodes.node_lab_fill_effect.handlers import HandlerLabFillOwnerFacts
from omnimarket.nodes.node_lab_fill_effect.models import (
    ModelLabFillOwnerFacts,
    ModelLabFillOwnerFactsRequest,
)
from omnimarket.nodes.node_lab_fill_effect.protocols import (
    LabFillClaimFacts,
    LabFillPortError,
)
from omnimarket.nodes.node_lab_fill_effect.protocols.local_lab_fill_adapters import (
    LocalOwnerReader,
)
from omnimarket.nodes.node_reading_writer_effect.adapters.ledger_adapters import (
    evidence_row_from_text,
)
from omnimarket.nodes.node_work_state_projection_compute.handlers.handler_work_state_projection import (
    HandlerWorkStateProjection,
)
from tests.nodes.node_lab_fill_effect.fakes import FakeClock, FakeOwners

pytestmark = pytest.mark.unit

EXAMPLE_OVERLAY = (
    Path(__file__).resolve().parents[3]
    / "src/omnimarket/nodes/node_work_state_projection_compute/overlay.example.yaml"
)
RULES = load_structured_overlay(
    "node_work_state_projection_compute",
    ModelWorkStateRules,
    explicit=yaml.safe_load(EXAMPLE_OVERLAY.read_text(encoding="utf-8")),
)
LANES = [
    {
        "lane": "fill-omn-7",
        "ticket": "OMN-7",
        "pr": "",
        "repo": "omnimarket",
        "kind": "ticket",
    },
]
PROJECTION_ID = "work_state"
PRODUCER = "work-state-projection"
MAX_AGE_S = 900


def work_state(now: datetime) -> ModelWorkStateResult:
    """A projection one live lane's CLAIM cell attributes ZEP-7 to, built the way the projection node builds it."""
    stamp = (now - timedelta(minutes=20)).strftime("%Y-%m-%dT%H:%M:%SZ")
    claim = evidence_row_from_text(
        f"{stamp} | CLAIM | lane=peer-lane | ticket=ZEP-7 | actor=claude | model=sonnet | working it"
    )
    assert claim is not None
    return HandlerWorkStateProjection().handle(
        ModelWorkStateRequest(
            now=now,
            rules=RULES,
            scope=ModelWorkScope(),
            tickets=(ModelTicketSource(ticket_id="ZEP-7", state_type="started"),),
            ledger_rows=(claim,),
        )
    )


def read_entry(
    payload: Mapping[str, object], *, as_of: datetime, now: datetime
) -> ModelFloorProjectionEntry:
    """A work_state entry that was read: fresh inside its own 900s bound, with the projection as its payload."""
    return ModelFloorProjectionEntry(
        projection_id=PROJECTION_ID,
        status="read",
        max_age_s=MAX_AGE_S,
        read_at=now,
        as_of=as_of,
        producer=PRODUCER,
        payload=dict(payload),
    )


def unread_entry(now: datetime, reason: str) -> ModelFloorProjectionEntry:
    return ModelFloorProjectionEntry(
        projection_id=PROJECTION_ID,
        status="unread",
        max_age_s=MAX_AGE_S,
        read_at=now,
        reason=reason,
    )


def bundle_file(
    directory: Path, entry: ModelFloorProjectionEntry, now: datetime
) -> Path:
    path = directory / "floor-bundle.json"
    path.write_text(
        ModelFloorProjectionBundle(
            read_at=now, entries={PROJECTION_ID: entry}
        ).model_dump_json(),
        encoding="utf-8",
    )
    return path


@dataclass
class BundleOwners(FakeOwners):
    """The claim store is empty; the work-state reading is the production adapter's, over the bundle or the file it is given."""

    def work_state_owners(
        self, state_path: str, max_age_s: float
    ) -> Mapping[str, Sequence[str]] | None:
        self.called.append("work_state")
        return LocalOwnerReader().work_state_owners(state_path, max_age_s)

    def work_state_owners_from_bundle(
        self, bundle_path: str
    ) -> Mapping[str, Sequence[str]] | None:
        self.called.append("work_state_bundle")
        return LocalOwnerReader().work_state_owners_from_bundle(bundle_path)


def facts_of(request: dict[str, object]) -> tuple[ModelLabFillOwnerFacts, BundleOwners]:
    owners = BundleOwners(
        claims_facts=LabFillClaimFacts(index={}, open_claims=[], staleness_hours=12),
        registry={},
    )
    facts = HandlerLabFillOwnerFacts(reader=owners, clock=FakeClock()).handle(
        ModelLabFillOwnerFactsRequest.model_validate(
            {"lanes": LANES, "ledger_path": "/l", **request}
        )
    )
    return facts, owners


def assert_not_a_free_ticket(path: Path, text: str) -> None:
    """The bundle reads as a LabFillPortError at the port, and as that error string in the facts, never an empty dict."""
    with pytest.raises(LabFillPortError, match=text):
        LocalOwnerReader().work_state_owners_from_bundle(str(path))
    facts, _ = facts_of({"work_state_bundle_path": str(path)})
    assert isinstance(facts.work_state_owners, str)
    assert text in facts.work_state_owners


def test_golden_a_named_bundle_serves_the_work_state_owners(tmp_path: Path) -> None:
    now = datetime.now(UTC)
    bundle = bundle_file(
        tmp_path,
        read_entry(
            work_state(now).model_dump(mode="json"),
            as_of=now - timedelta(seconds=60),
            now=now,
        ),
        now,
    )
    assert LocalOwnerReader().work_state_owners_from_bundle(str(bundle)) == {
        "ZEP-7": ["peer-lane"]
    }
    facts, owners = facts_of({"work_state_bundle_path": str(bundle)})
    assert facts.work_state_owners == {"ZEP-7": ("peer-lane",)}
    assert owners.called == ["claims", "work_state_bundle"]


def test_golden_an_empty_bundle_path_reads_the_projection_file(tmp_path: Path) -> None:
    now = datetime.now(UTC)
    state = tmp_path / "work-state.json"
    state.write_text(work_state(now).model_dump_json(), encoding="utf-8")
    facts, owners = facts_of({"work_state_path": str(state)})
    assert facts.work_state_owners == {"ZEP-7": ("peer-lane",)}
    assert owners.called == ["claims", "work_state"]


def test_error_an_entry_over_its_bound_is_not_a_free_ticket(tmp_path: Path) -> None:
    now = datetime.now(UTC)
    bundle = bundle_file(
        tmp_path,
        read_entry(
            work_state(now).model_dump(mode="json"),
            as_of=now - timedelta(seconds=1000),
            now=now,
        ),
        now,
    )
    assert_not_a_free_ticket(bundle, "over its")


def test_error_an_unread_entry_is_not_a_free_ticket(tmp_path: Path) -> None:
    now = datetime.now(UTC)
    bundle = bundle_file(
        tmp_path,
        unread_entry(now, "the read window closed without the snapshot"),
        now,
    )
    assert_not_a_free_ticket(bundle, "the read window closed without the snapshot")


def test_error_an_absent_bundle_is_not_a_free_ticket(tmp_path: Path) -> None:
    assert_not_a_free_ticket(
        tmp_path / "absent-bundle.json", "work-state bundle absent"
    )


def test_error_a_file_that_is_not_a_bundle_is_not_a_free_ticket(tmp_path: Path) -> None:
    path = tmp_path / "floor-bundle.json"
    path.write_text(json.dumps({"read_at": "2026-09-21T14:13:20Z"}), encoding="utf-8")
    assert_not_a_free_ticket(path, "work-state bundle is not readable")


def test_error_a_payload_that_is_not_a_projection_is_not_a_free_ticket(
    tmp_path: Path,
) -> None:
    now = datetime.now(UTC)
    bundle = bundle_file(
        tmp_path,
        read_entry(
            {"snapshot": "of another shape"},
            as_of=now - timedelta(seconds=60),
            now=now,
        ),
        now,
    )
    assert_not_a_free_ticket(bundle, "work-state projection is not readable")


def test_seam_the_bundle_and_the_file_agree_until_the_payload_key_is_renamed(
    tmp_path: Path,
) -> None:
    now = datetime.now(UTC)
    result = work_state(now)
    state = tmp_path / "work-state.json"
    state.write_text(result.model_dump_json(), encoding="utf-8")
    bundle = bundle_file(
        tmp_path,
        read_entry(
            result.model_dump(mode="json"),
            as_of=now - timedelta(seconds=60),
            now=now,
        ),
        now,
    )
    reader = LocalOwnerReader()
    assert reader.work_state_owners_from_bundle(
        str(bundle)
    ) == reader.work_state_owners(str(state), MAX_AGE_S)

    raw = json.loads(bundle.read_text(encoding="utf-8"))
    payload = raw["entries"][PROJECTION_ID]["payload"]
    raw["entries"][PROJECTION_ID]["payload"] = {
        ("itemz" if key == "items" else key): value for key, value in payload.items()
    }
    bundle.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(LabFillPortError, match="work-state projection is not readable"):
        reader.work_state_owners_from_bundle(str(bundle))
