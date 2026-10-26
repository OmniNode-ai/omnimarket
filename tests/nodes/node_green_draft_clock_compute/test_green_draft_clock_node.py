"""The green PR draft clock used by the throughput tick (OMN-19998)."""

from __future__ import annotations

import importlib
from collections import UserDict
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[3]
NODE = "omnimarket.nodes.node_green_draft_clock_compute"
handler = importlib.import_module(f"{NODE}.handler")
models = importlib.import_module(f"{NODE}.models")
NOW = datetime(2026, 10, 19, 0, 53, tzinfo=UTC)
EARLIER = NOW - timedelta(minutes=7)
STAMP = EARLIER.strftime("%Y-%m-%dT%H:%M:%SZ")


def clock(previous, drafts_now):
    return handler.HandlerGreenDraftClock().handle(
        models.ModelGreenDraftClockRequest(previous, drafts_now, NOW)
    )


def test_green_since_uses_the_newest_valid_run_stamp():
    pr = {
        "ci": {
            "runs": [
                ["older", "completed", "success", "2026-10-18T18:00:00Z"],
                ["newest", "completed", "success", "2026-10-18T18:12:23Z"],
                ["naive", "completed", "success", "2026-10-19T00:50:00"],
                ["invalid", "completed", "success", "unparsable"],
                ["short"],
                None,
            ]
        },
        "first_seen": "2026-10-19T00:51:00Z",
    }
    assert handler.green_since(pr) == datetime(2026, 10, 18, 18, 12, 23, tzinfo=UTC)


@pytest.mark.parametrize(
    "ci",
    [None, [], {}, {"runs": []}, {"runs": [["check", "completed", "success", None]]}],
)
def test_green_since_falls_back_to_first_seen_when_no_run_carries_a_stamp(ci):
    assert handler.green_since({"ci": ci, "first_seen": STAMP}) == EARLIER


def test_a_later_floor_raises_the_clock_start():
    assert handler.green_since({"first_seen": STAMP}, NOW) == NOW


def test_an_earlier_floor_does_not_lower_the_clock_start():
    assert (
        handler.green_since({"first_seen": STAMP}, EARLIER - timedelta(hours=1))
        == EARLIER
    )


@pytest.mark.parametrize("stamp", [None, "", "invalid", "2026-10-19T00:46:00", 42])
def test_no_valid_stamp_stays_none_even_with_a_floor(stamp):
    pr = {
        "ci": {"runs": [["check", "completed", "success", stamp]]},
        "first_seen": stamp,
    }
    assert handler.green_since(pr) is None
    assert handler.green_since(pr, NOW) is None


def test_a_draft_records_now_and_an_open_non_draft_keeps_its_previous_stamp():
    previous = {"draft": STAMP, "ready": STAMP, "closed": STAMP}
    drafts_now = {"draft": True, "ready": False, "unseen-ready": False}
    result = clock(previous, drafts_now)
    assert result.drafts == {
        "draft": NOW.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "ready": STAMP,
    }
    assert result.ready_since == {"draft": NOW, "ready": EARLIER}
    assert previous == {"draft": STAMP, "ready": STAMP, "closed": STAMP}
    assert drafts_now == {"draft": True, "ready": False, "unseen-ready": False}


@pytest.mark.parametrize("previous", [None, [], ["pr"], "invalid", 42])
def test_a_non_mapping_previous_is_empty(previous):
    result = clock(previous, {"draft": True, "ready": False})
    assert result.drafts == {"draft": NOW.strftime("%Y-%m-%dT%H:%M:%SZ")}
    assert result.ready_since == {"draft": NOW}


def test_a_mapping_previous_is_accepted_and_invalid_stamps_are_kept_but_not_parsed():
    previous = UserDict(
        {"ready": STAMP, "bad": "invalid", "naive": "2026-10-19T00:46:00"}
    )
    result = clock(previous, {"ready": False, "bad": False, "naive": False})
    assert result.drafts == previous
    assert result.ready_since == {"ready": EARLIER}


def test_the_node_convenience_function_and_reexport_match_the_handler():
    node = importlib.import_module(f"{NODE}.node")
    result = clock({"ready": STAMP}, {"draft": True, "ready": False})
    assert node.draft_clock({"ready": STAMP}, {"draft": True, "ready": False}, NOW) == (
        result.drafts,
        result.ready_since,
    )
    assert node.green_since is handler.green_since


def test_the_contract_names_the_handler_and_models_this_package_defines():
    contract = yaml.safe_load(
        (
            ROOT / "src/omnimarket/nodes/node_green_draft_clock_compute/contract.yaml"
        ).read_text()
    )
    assert contract["name"] == "node_green_draft_clock_compute"
    assert getattr(handler, contract["handler"]["class"])
    assert getattr(models, contract["input_model"]["name"])
    assert getattr(models, contract["output_model"]["name"])
