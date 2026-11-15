# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Error chain: an unreachable peer, a parked host, no peers, the closed request and the failing control (OMN-24279)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from omnimarket.nodes.node_lab_ssh_probe_compute.handlers import HandlerLabSshProbe
from omnimarket.nodes.node_lab_ssh_probe_compute.handlers.handler_lab_ssh_probe import (
    octal,
)
from omnimarket.nodes.node_lab_ssh_probe_compute.models import ModelLabSshProbeRequest
from tests.nodes.node_lab_ssh_probe_compute.probe_world import (
    H202_ROWS,
    home,
    render,
    request,
    row,
    run_probe,
)


def test_a_peer_none_of_whose_destinations_answers_reads_missing_and_names_only_it() -> (
    None
):
    top, env = home(("hp-gen9",))
    line = run_probe(render("h202", H202_ROWS).script, top, env)
    assert line == "PARITY-CAP lab-ssh missing no-ssh-route-to:h051"


def test_a_parked_host_is_never_named_and_never_makes_the_line_missing() -> None:
    """h106 (omnidad.local) parked on 2026-11-13: a probe built from an older table read ``no-ssh-route-to:omnidad.local``."""
    rows = (*H202_ROWS, row("h106", "omnidad.local", available=False))
    rendered = render("h202", rows)
    assert "omnidad.local" not in rendered.peers
    top, env = home(("hp-gen8", "hp-gen9"))
    line = run_probe(rendered.script, top, env)
    assert line == "PARITY-CAP lab-ssh ok reached:hp-gen8,hp-gen9"
    assert "omnidad" not in rendered.script


def test_a_host_alone_in_the_table_or_with_every_peer_parked_reads_unknown() -> None:
    rows = (
        row("h202", "h202-addr"),
        row("h106", "omnidad.local", available=False),
    )
    rendered = render("h202", rows)
    assert rendered.peers == ()
    top, env = home(())
    assert (
        run_probe(rendered.script, top, env)
        == "PARITY-CAP lab-ssh unknown no-peers-named"
    )


def test_the_host_is_never_its_own_peer() -> None:
    assert render("h051", H202_ROWS).peers == ("h202-addr", "h053")


def test_a_destination_off_the_grammar_is_dropped_and_a_row_whose_target_is_off_it_is_not_a_peer() -> (
    None
):
    rows = (
        row("h202", "h202-addr"),
        row("h051", "h051", "bad name; rm", "hp-gen8"),
        row("h053", "h053 && x"),
    )
    rendered = render("h202", rows)
    assert rendered.peers == ("h051",)
    assert octal("bad name; rm") not in rendered.script
    assert octal("h053 && x") not in rendered.script
    assert octal("hp-gen8") in rendered.script


def test_the_request_is_closed_and_every_bound_is_the_callers() -> None:
    good = request("h202", H202_ROWS).model_dump()
    with pytest.raises(ValidationError):
        ModelLabSshProbeRequest(**good, extra_field=1)
    for field in (
        "check_s",
        "connect_s",
        "fresh_min",
        "lock_min",
        "bound_sh",
        "cache_dir",
    ):
        short = {k: v for k, v in good.items() if k != field}
        with pytest.raises(ValidationError):
            ModelLabSshProbeRequest(**short)
    with pytest.raises(ValidationError):
        request("h202", H202_ROWS, check_s=0)


def test_failing_control_without_the_fallback_loop_the_h202_case_reads_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Remove what the golden test reads: with each peer tried at its target only, h202 reads missing, as recorded on 2026-11-13."""
    stripped = tuple(r.model_copy(update={"fallback_targets": ()}) for r in H202_ROWS)
    top, env = home(("hp-gen8", "hp-gen9"))
    line = run_probe(
        HandlerLabSshProbe().handle(request("h202", stripped)).script, top, env
    )
    assert line == "PARITY-CAP lab-ssh missing no-ssh-route-to:h051,h053"


def test_failing_control_with_the_parked_filter_removed_the_parked_host_breaks_the_line() -> (
    None
):
    unparked = (*H202_ROWS, row("h106", "omnidad.local", available=True))
    top, env = home(("hp-gen8", "hp-gen9"))
    line = run_probe(render("h202", unparked).script, top, env)
    assert line == "PARITY-CAP lab-ssh missing no-ssh-route-to:omnidad.local"
