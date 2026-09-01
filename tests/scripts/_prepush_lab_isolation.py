# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Keep hook-subprocess tests from spending a real lab host's cores (OMN-16991).

Several tests in this directory run the REAL pre-push hook with the heavy
escalation forced (``PREPUSH_FULL_SUITE=1``) and every designated host
de-designated, to prove the refusal. Before OMN-17435 that was harmless in this
repo for a structural reason: the hook had no lab-dispatch seam at all, so there
was no remote host it could reach.

This port removes that accidental containment. The equivalent was observed live
in omnibase_infra on 2026-08-30, minutes after its scan was fixed: `pytest`
shipped a real git bundle to ``omnibook``, took that host's exclusive slot, and
started a full suite there -- with ORIGIN on the remote wrapper naming the test
process itself. That is the OMN-16425/OMN-16489 F-01 recursion in its
distributed form, reached from a unit test instead of a push, and it burns a lab
host for as long as the suite runs.

The isolation below uses the picker's OWN deterministic override surface rather
than a new knob. ``PREPUSH_SLOT_OVERRIDE_MAP`` is consulted before any network
call, and a label absent from the map resolves to "slot unknown", which the
picker treats as unfit and skips -- the same fail-closed posture it applies to
an unreachable host. A map naming no real label therefore makes EVERY row unfit
with zero ssh, and stays correct when a row is added to
``scripts/hooks/prepush_hosts.tsv``.

It can only make the gate stricter. With no host placeable the lab leg produces
no evidence and the hook falls through to its pre-existing precedence (grant ->
die), which is exactly what these tests assert.
"""

from __future__ import annotations

#: Deliberately names no real row label. See the module docstring.
LAB_ISOLATION_ENV = {"PREPUSH_SLOT_OVERRIDE_MAP": "no-such-host=unknown"}


def network_free_lab_env() -> dict[str, str]:
    """Env fragment that makes the lab-dispatch leg network-free."""
    return dict(LAB_ISOLATION_ENV)
