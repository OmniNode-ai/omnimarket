# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""The package announcer publishes one announcement per contract (OMN-21597, A1 AC1, AC4)."""

import hashlib
from pathlib import Path

import pytest
from omnibase_core.enums.enum_announce_refusal_reason import EnumAnnounceRefusalReason
from omnibase_core.models.events.contract_registration.model_node_announcement import (
    ModelNodeAnnouncement,
)

from omnimarket.nodes.node_package_announce_effect.announce_step0 import (
    AnnounceStep0,
)
from tests.nodes.node_package_announce_effect.helper_announce_buses import (
    AnnounceTestBindings,
    make_overlay,
)
from tests.nodes.node_package_announce_effect.helper_announce_site import (
    contract_text,
    write_package,
)


def _canonical_sha256(raw: bytes) -> str:
    """Independent restatement of the canonical form, for comparison."""
    return hashlib.sha256(raw.replace(b"\r\n", b"\n").replace(b"\r", b"\n")).hexdigest()


@pytest.mark.unit
async def test_two_packages_three_contracts_publish_exactly_three_announcements(
    bindings: AnnounceTestBindings,
    standard_contracts: dict[str, bytes],
    bus_binding: str,
) -> None:
    step0 = AnnounceStep0(overlay=make_overlay(bus_binding), bindings=bindings.registry)
    await step0.run()

    published = [
        ModelNodeAnnouncement.model_validate_json(value)
        for _, _, value in bindings.last_bus.published
    ]
    assert len(published) == 3, "falsifier: a count other than three"
    by_name = {record.entity_id: record for record in published}
    assert set(by_name) == set(standard_contracts)
    for name, raw in standard_contracts.items():
        assert by_name[name].content_hash == _canonical_sha256(raw), (
            f"falsifier: a hash that differs from the canonical form's ({name})"
        )


@pytest.mark.unit
async def test_windows_line_endings_hash_like_unix_line_endings(
    bindings: AnnounceTestBindings,
    standard_contracts: dict[str, bytes],
    bus_binding: str,
) -> None:
    crlf = standard_contracts["fixture_one"]
    assert b"\r\n" in crlf
    step0 = AnnounceStep0(overlay=make_overlay(bus_binding), bindings=bindings.registry)
    catalog = await step0.run()
    entry = catalog.lookup("fixture_one")
    assert entry is not None
    assert (
        entry.content_hash == hashlib.sha256(crlf.replace(b"\r\n", b"\n")).hexdigest()
    )
    assert entry.content_hash != hashlib.sha256(crlf).hexdigest()


@pytest.mark.unit
async def test_announcement_carries_package_topics_and_a_per_workload_topic(
    bindings: AnnounceTestBindings,
    standard_contracts: dict[str, bytes],
    bus_binding: str,
) -> None:
    step0 = AnnounceStep0(overlay=make_overlay(bus_binding), bindings=bindings.registry)
    await step0.run()
    topic, _, value = next(
        item for item in bindings.last_bus.published if b'"fixture_three"' in item[2]
    )
    record = ModelNodeAnnouncement.model_validate_json(value)
    assert topic == "onex.evt.platform.node-announcement-from-fixture.v1"
    assert (record.package.distribution, record.package.version) == (
        "beta-pkg",
        "2.1.0",
    )
    assert record.declared_topics == (
        "onex.cmd.fixture.fixture-three-requested.v1",
        "onex.evt.fixture.fixture-three-completed.v1",
    )
    assert record.generation == 1


@pytest.mark.unit
async def test_planted_malformed_contracts_are_refused_with_typed_reasons_and_no_entry(
    site: Path,
    bindings: AnnounceTestBindings,
    bus_binding: str,
) -> None:
    """Planted violations: unreadable YAML, a contract with no name, a listed file that is absent."""
    good = contract_text("fixture_good", handler_module="alpha_pkg.nodes.good.handler")
    write_package(
        site,
        "alpha-pkg",
        "1.0.0",
        {
            "fixture_good": good,
            "planted_unreadable": b"name: [unterminated\n  : : :\n",
            "planted_nameless": b"node_type: compute\n",
            "planted_not_a_mapping": b"- just\n- a\n- list\n",
        },
    )
    (site / "alpha_pkg" / "nodes" / "planted_nameless").joinpath(
        "contract.yaml"
    ).write_bytes(b"node_type: compute\n")
    step0 = AnnounceStep0(overlay=make_overlay(bus_binding), bindings=bindings.registry)
    catalog = await step0.run()

    report = step0.report
    assert report is not None
    assert report.announced == ("fixture_good",)
    assert report.refused["alpha-pkg:planted_unreadable"] is (
        EnumAnnounceRefusalReason.CONTRACT_UNREADABLE
    )
    assert report.refused["alpha-pkg:planted_not_a_mapping"] is (
        EnumAnnounceRefusalReason.CONTRACT_UNREADABLE
    )
    assert report.refused["alpha-pkg:planted_nameless"] is (
        EnumAnnounceRefusalReason.CONTRACT_NAME_MISSING
    )
    assert set(catalog.snapshot()) == {"fixture_good"}, "falsifier: an entry exists"
    assert len(bindings.last_bus.published) == 1


@pytest.mark.unit
async def test_a_listed_contract_whose_file_is_absent_is_refused(
    site: Path,
    bindings: AnnounceTestBindings,
    bus_binding: str,
) -> None:
    write_package(
        site,
        "alpha-pkg",
        "1.0.0",
        {
            "planted_missing": contract_text(
                "planted_missing", handler_module="alpha_pkg.x"
            )
        },
    )
    (site / "alpha_pkg" / "nodes" / "planted_missing" / "contract.yaml").unlink()
    step0 = AnnounceStep0(overlay=make_overlay(bus_binding), bindings=bindings.registry)
    catalog = await step0.run()
    assert step0.report is not None
    assert step0.report.refused == {
        "alpha-pkg:planted_missing": EnumAnnounceRefusalReason.CONTRACT_FILE_MISSING
    }
    assert catalog.snapshot() == {}


@pytest.mark.unit
async def test_unacknowledged_announcement_is_retried_then_refused(
    bindings: AnnounceTestBindings,
    standard_contracts: dict[str, bytes],
) -> None:
    """The contract's retry transition allows three retries: four publishes per contract."""
    step0 = AnnounceStep0(overlay=make_overlay("blackhole"), bindings=bindings.registry)
    catalog = await step0.run()
    assert step0.report is not None
    assert set(step0.report.refused.values()) == {
        EnumAnnounceRefusalReason.ACKNOWLEDGEMENT_TIMEOUT
    }
    assert len(bindings.last_bus.published) == 3 * 4
    assert catalog.snapshot() == {}
