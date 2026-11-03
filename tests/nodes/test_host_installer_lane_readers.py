# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-22100 AC3: a declared lane host that lacks the catalog path or the lab env file fails, and the committed table passes.

On 2026-10-31 a lab host answered "no catalog.json given" to ``onex-capability-catalog find`` and had no database password for the
work-ledger parity check, so two readers stayed unread. The host installer owns those inputs; the committed table here declares them
for every lane host and this test refuses a row, or a measured host reading, that leaves one out. Key names only; no value is read.
"""

from __future__ import annotations

import pytest

from omnimarket.nodes.node_host_capability_compute.handlers.handler_lane_reader_inputs import (
    CATALOG_ENV,
    CATALOG_FIND,
    LEDGER_PARITY,
    TABLE_PATH,
    WL_ENV,
    WL_PASSWORD_KEY,
    HandlerLaneReaderInputs,
    parse_declared_hosts,
)
from omnimarket.nodes.node_host_capability_compute.models import (
    ModelLaneReaderHost,
    ModelLaneReaderInputsRequest,
)

PROOF_HOST = "h051"


def load_declared_hosts() -> tuple[ModelLaneReaderHost, ...]:
    return parse_declared_hosts(TABLE_PATH.read_text(encoding="utf-8"))


def judge(*hosts: ModelLaneReaderHost) -> list[tuple[str, str, str]]:
    result = HandlerLaneReaderInputs().handle(
        ModelLaneReaderInputsRequest(hosts=tuple(hosts))
    )
    return [(f.host, f.reader, f.item) for f in result.failures]


def test_the_committed_host_table_declares_both_inputs_for_every_lane_host() -> None:
    hosts = load_declared_hosts()
    assert PROOF_HOST in {h.host for h in hosts}
    assert len({h.host for h in hosts}) == len(hosts)
    assert judge(*hosts) == []


def test_every_committed_path_is_relative_to_the_lane_home_and_holds_no_value() -> None:
    for h in load_declared_hosts():
        for path in (h.catalog_json, h.wl_env_file):
            assert path
            assert not path.startswith(("/", "~"))
            assert ".." not in path
            assert "=" not in path


@pytest.mark.parametrize(
    ("drop", "reader", "item"),
    [
        ("catalog_json", CATALOG_FIND, CATALOG_ENV),
        ("wl_env_file", LEDGER_PARITY, WL_ENV),
    ],
)
def test_a_declared_host_without_either_input_fails_and_only_that_host(
    drop: str, reader: str, item: str
) -> None:
    hosts = load_declared_hosts()
    broken = [
        h.model_copy(update={drop: None}) if h.host == PROOF_HOST else h for h in hosts
    ]
    assert judge(*broken) == [(PROOF_HOST, reader, item)]


def test_a_host_missing_both_inputs_fails_for_both_readers() -> None:
    assert judge(ModelLaneReaderHost(host="bare")) == [
        ("bare", CATALOG_FIND, CATALOG_ENV),
        ("bare", LEDGER_PARITY, WL_ENV),
    ]


def test_the_h051_reading_of_2026_11_02_fails_on_the_missing_password_key_by_name() -> (
    None
):
    # Names only, as measured on the host: its lab env file held the Infisical identity keys and no database password.
    measured = (
        "INFISICAL_ADDR",
        "INFISICAL_CLIENT_ID",
        "INFISICAL_CLIENT_SECRET",
        "INFISICAL_PROJECT_ID",
        "INFISICAL_ENVIRONMENT_SLUG",
    )
    host = next(h for h in load_declared_hosts() if h.host == PROOF_HOST)
    reading = host.model_copy(
        update={"wl_env_key_names": measured, "catalog_json_present": False}
    )
    assert judge(reading) == [
        (PROOF_HOST, CATALOG_FIND, host.catalog_json),
        (PROOF_HOST, LEDGER_PARITY, WL_PASSWORD_KEY),
    ]
    provisioned = reading.model_copy(
        update={
            "wl_env_key_names": (*measured, WL_PASSWORD_KEY),
            "catalog_json_present": True,
        }
    )
    assert judge(provisioned) == []


def test_an_unmeasured_reading_is_never_a_failure() -> None:
    host = next(h for h in load_declared_hosts() if h.host == PROOF_HOST)
    assert host.wl_env_key_names is None
    assert host.catalog_json_present is None
    assert judge(host) == []
