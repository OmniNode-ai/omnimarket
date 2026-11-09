# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The alarm reads the real lab host table: a list of rows, not a mapping of host to names (OMN-22778).

The deployment's host table is ``hosts:`` holding a list of rows (``name``, ``target``, ``fallback_targets``, ...).
The alarm's reader wanted ``hosts:`` as a mapping of host id to ``hostnames`` and refused the real table with
"no hosts mapping", so no event could be attributed. On 2026-11-08 one pool host sent tens of thousands of hook events in a day under its
native machine name while its row named it only by another name, and the alarm named it quiet four times.
Host names here are synthetic; the real table is deployment data. Each reading has a control over the same table
without the declaration, which must read the host quiet: the live reading depends on the declared name.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from omnimarket.models.overlay_keys.model_overlay_documents import (
    ModelLabHostRow,
    ModelLabHostsTable,
)
from omnimarket.nodes.node_silent_failure_alarm_effect.host_table import (
    HostTableError,
    read_host_aliases,
)
from omnimarket.nodes.node_silent_failure_alarm_effect.models import (
    ModelSilentFailureAlarmOptions,
)
from tests.nodes.node_silent_failure_alarm_effect.test_host_table_aliases_omn22755 import (
    _tick,
)

pytestmark = pytest.mark.unit

# The shape of the deployment table: rows with a name and the ways the host is reached. h051 is reached by an ssh
# alias, h053 by an alias with a second tailnet name, h101 by an address with a tailnet name, h105 by a login at a
# machine name, h106 by a ``.local`` name and an address. Only some rows name the machine the events carry.
REAL_ROWS = """\
hosts:
- name: h051
  target: alpha-alias
  hostnames: [alpha-box]
- name: h053
  target: beta-alias
  fallback_targets: [beta-box.example.test]
  cap: 12
- name: h101
  target: 192.0.2.101
  fallback_targets: [gamma-two]
  hostnames: [Gamma.local]
- name: h105
  target: lab@delta
  available: false
- name: h106
  target: epsilon.local
  fallback_targets: [192.0.2.106]
unit_needs: []
registry_root_candidates: [/srv/omni]
"""


def _real_table(tmp_path: Path, text: str = REAL_ROWS) -> Path:
    path = tmp_path / "lab_hosts.yaml"
    path.write_text(text)
    return path


def test_real_table_yields_each_hosts_event_labels(tmp_path: Path) -> None:
    aliases = read_host_aliases(_real_table(tmp_path))
    # The declared native name comes first, then the names the row is reached by.
    assert aliases["h051"] == ("alpha-box", "alpha-alias")
    assert aliases["h053"] == ("beta-alias", "beta-box")
    # An address is not a name any event carries; the declared name and the tailnet name are.
    assert aliases["h101"] == ("gamma", "gamma-two")
    # ``login@machine`` names the machine after the login, and an unavailable host is still attributed.
    assert aliases["h105"] == ("delta",)
    assert aliases["h106"] == ("epsilon",)
    assert set(aliases) == {"h051", "h053", "h101", "h105", "h106"}


def test_real_table_loads_as_the_lab_host_tables_own_model(tmp_path: Path) -> None:
    """The declaration cannot drift from the model every other consumer validates the table with."""
    import yaml

    table = ModelLabHostsTable.model_validate(
        yaml.safe_load(_real_table(tmp_path).read_text())
    )
    assert next(h for h in table.hosts if h.name == "h101").hostnames == (
        "Gamma.local",
    )
    assert ModelLabHostRow(name="h900", target="h900").hostnames == ()


def test_alarm_options_read_the_real_table(tmp_path: Path) -> None:
    options = ModelSilentFailureAlarmOptions(
        registry_root=tmp_path, host_table=_real_table(tmp_path)
    )
    assert options.host_table_error == ""
    assert options.host_aliases["h101"] == ("gamma", "gamma-two")


def test_event_under_the_declared_native_name_is_not_quiet(tmp_path: Path) -> None:
    """h101: the events carry ``Gamma.local``; the row reaches it as gamma-two."""
    quiet, summary, warnings = _tick(tmp_path, _real_table(tmp_path), "Gamma.local")
    assert not [w for w in warnings if "host table" in w]
    assert "quiet_host=UNKNOWN" not in summary
    assert "h101" not in quiet
    assert "h051" in quiet  # no event of its own: the reading is still per host


def test_event_under_a_name_the_row_does_not_give_reads_quiet_control(
    tmp_path: Path,
) -> None:
    """The failing control: the same table without h101's declared native name."""
    without = REAL_ROWS.replace("  hostnames: [Gamma.local]\n", "")
    quiet, _, _ = _tick(tmp_path, _real_table(tmp_path, without), "Gamma.local")
    assert "h101" in quiet


def test_real_table_with_a_row_that_has_no_name_is_refused(tmp_path: Path) -> None:
    with pytest.raises(HostTableError, match="has no name"):
        read_host_aliases(_real_table(tmp_path, "hosts:\n- target: alpha-alias\n"))


def test_real_table_giving_one_label_to_two_hosts_is_refused(tmp_path: Path) -> None:
    clash = REAL_ROWS.replace("target: epsilon.local", "target: delta")
    with pytest.raises(HostTableError, match="belongs to h105 and h106"):
        read_host_aliases(_real_table(tmp_path, clash))


def test_real_table_with_a_host_declared_twice_is_refused(tmp_path: Path) -> None:
    twice = "hosts:\n- name: h051\n  target: a\n- name: h051\n  target: b\n"
    with pytest.raises(HostTableError, match="declared twice"):
        read_host_aliases(_real_table(tmp_path, twice))


def test_real_table_with_only_addresses_gives_no_name_and_says_so(
    tmp_path: Path,
) -> None:
    only = "hosts:\n- name: h900\n  target: 192.0.2.9\n"
    with pytest.raises(HostTableError, match="no host gives a machine name"):
        read_host_aliases(_real_table(tmp_path, only))


def test_a_table_with_no_hosts_names_both_shapes(tmp_path: Path) -> None:
    with pytest.raises(HostTableError, match="no hosts mapping or list of host rows"):
        read_host_aliases(_real_table(tmp_path, "unit_needs: []\n"))
