# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The lab etcd member set is an overlay document with the rules the provisioning script expects (OMN-24562).

Five voters in the shape of the lab etcd provisioning's members.tsv are the known good document; a sixth voter,
a repeated name, a second learner and a second laptop voter are the known bad ones. The render test compares the
overlay's text with the voter rows of members.tsv, spelled here, so the two can be compared with no checkout of
the provisioning.
"""
# onex-allow-file OMN-24562 reason="invented tailnet-range addresses and host names as test fixtures; no real host or address"

from __future__ import annotations

import copy
import ipaddress
from typing import Any

import pytest
from pydantic import ValidationError

from omnimarket.models.overlay_documents.model_etcd_lab_members_overlay import (
    MEMBERS_TSV_HEADER,
    ModelEtcdLabMembersOverlay,
)


def _tn(n: int) -> str:
    """The n-th address of the shared address block a tailnet hands out, built from integers (no real host)."""
    return str(ipaddress.ip_address((100 << 24) | (64 << 16) | n))


# Five voters in the shape of members.tsv rows (name, ssh_target, os, tailnet_ip, dns_name, data_dir, unit), with invented
# names and addresses; the overlay home's own test holds the same check over the real five.
KNOWN_TSV_ROWS = [
    f"ha\tlab_run_hosts\tdarwin\t{_tn(1)}\tdns-a\t~/.local/share/etcd-lab\tlaunchd",
    f"hb\tlab_run_hosts\tdarwin\t{_tn(2)}\tdns-b\t~/.local/share/etcd-lab\tlaunchd",
    f"hc\tlab_run_hosts\tdarwin\t{_tn(3)}\tdns-c\t~/.local/share/etcd-lab\tlaunchd",
    f"hd\tlocal\tlinux\t{_tn(4)}\tdns-d\t/data/etcd-lab\tsystemd",
    f"he\tlab_run_hosts\tlinux\t{_tn(5)}\tdns-e\t~/.local/share/etcd-lab\tsystemd-user",
]


def _member(row: str, *, laptop: bool = False) -> dict[str, Any]:
    name, target, os_name, ip, dns, data, unit = row.split("\t")
    return {
        "name": name,
        "ssh_target": target,
        "os": os_name,
        "tailnet_ip": ip,
        "dns_name": dns,
        "data_dir": data,
        "unit": unit,
        "client_port": 2379,
        "peer_port": 2380,
        "role": "voter",
        "laptop": laptop,
        "host_ref": name,
    }


def _doc() -> dict[str, Any]:
    members = [_member(r, laptop=r.startswith("hc")) for r in KNOWN_TSV_ROWS]
    return {"members": members}


def _learner(name: str = "hf", ip: str = _tn(6)) -> dict[str, Any]:
    return {
        **_member(KNOWN_TSV_ROWS[3]),
        "name": name,
        "ssh_target": "lab_run_hosts",
        "tailnet_ip": ip,
        "dns_name": f"dns-{name}",
        "role": "learner",
        "host_ref": name,
    }


def test_the_current_five_voters_load() -> None:
    doc = ModelEtcdLabMembersOverlay.model_validate(_doc())
    assert [m.name for m in doc.voters()] == ["ha", "hb", "hc", "hd", "he"]
    assert doc.learners() == ()


def test_render_is_the_text_of_members_tsv() -> None:
    doc = ModelEtcdLabMembersOverlay.model_validate(_doc())
    assert (
        doc.render_members_tsv()
        == "\n".join([MEMBERS_TSV_HEADER, *KNOWN_TSV_ROWS]) + "\n"
    )


def test_a_learner_is_not_a_row_of_members_tsv() -> None:
    raw = _doc()
    raw["members"].append(_learner())
    doc = ModelEtcdLabMembersOverlay.model_validate(raw)
    assert [m.name for m in doc.learners()] == ["hf"]
    assert (
        doc.render_members_tsv()
        == "\n".join([MEMBERS_TSV_HEADER, *KNOWN_TSV_ROWS]) + "\n"
    )


def test_six_voters_are_refused() -> None:
    raw = _doc()
    raw["members"].append({**_learner(), "role": "voter"})
    with pytest.raises(ValidationError, match="exactly five voters"):
        ModelEtcdLabMembersOverlay.model_validate(raw)


def test_four_voters_are_refused() -> None:
    raw = _doc()
    raw["members"].pop()
    with pytest.raises(ValidationError, match="exactly five voters"):
        ModelEtcdLabMembersOverlay.model_validate(raw)


def test_a_repeated_name_is_refused() -> None:
    raw = _doc()
    raw["members"][1]["name"] = "ha"
    with pytest.raises(ValidationError, match="name repeats"):
        ModelEtcdLabMembersOverlay.model_validate(raw)


def test_a_repeated_tailnet_address_is_refused() -> None:
    raw = _doc()
    raw["members"][1]["tailnet_ip"] = raw["members"][0]["tailnet_ip"]
    with pytest.raises(ValidationError, match="tailnet_ip repeats"):
        ModelEtcdLabMembersOverlay.model_validate(raw)


def test_two_learners_are_refused() -> None:
    raw = _doc()
    raw["members"].extend([_learner("hf", _tn(6)), _learner("hg", _tn(7))])
    with pytest.raises(ValidationError, match="at most one learner"):
        ModelEtcdLabMembersOverlay.model_validate(raw)


def test_a_second_laptop_voter_is_refused() -> None:
    raw = _doc()
    raw["members"][1]["laptop"] = True
    with pytest.raises(ValidationError, match="laptop"):
        ModelEtcdLabMembersOverlay.model_validate(raw)


def test_a_laptop_learner_is_allowed() -> None:
    raw = _doc()
    raw["members"].append({**_learner(), "laptop": True})
    ModelEtcdLabMembersOverlay.model_validate(raw)


def test_an_address_off_the_tailnet_is_refused() -> None:
    raw = _doc()
    raw["members"][0]["tailnet_ip"] = str(ipaddress.ip_address(10 << 24 | 5))
    with pytest.raises(ValidationError, match="tailnet"):
        ModelEtcdLabMembersOverlay.model_validate(raw)


def test_a_unit_the_os_cannot_run_is_refused() -> None:
    raw = _doc()
    raw["members"][0]["unit"] = "systemd"
    with pytest.raises(ValidationError, match="unit"):
        ModelEtcdLabMembersOverlay.model_validate(raw)


def test_client_and_peer_port_must_differ() -> None:
    raw = _doc()
    raw["members"][0]["peer_port"] = 2379
    with pytest.raises(ValidationError, match="ports"):
        ModelEtcdLabMembersOverlay.model_validate(raw)


def test_an_unknown_key_is_refused() -> None:
    raw = _doc()
    raw["members"][0]["prod"] = True
    with pytest.raises(ValidationError):
        ModelEtcdLabMembersOverlay.model_validate(raw)


@pytest.mark.parametrize("key", sorted(_member(KNOWN_TSV_ROWS[0])))
def test_a_member_missing_a_key_is_refused(key: str) -> None:
    raw = copy.deepcopy(_doc())
    del raw["members"][0][key]
    with pytest.raises(ValidationError):
        ModelEtcdLabMembersOverlay.model_validate(raw)
