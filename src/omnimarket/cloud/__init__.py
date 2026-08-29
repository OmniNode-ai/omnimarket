# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Tenant-facing delegation over the OmniNode gateway's HTTPS surface (OMN-16967).

This package is the CUSTOMER path, and it is deliberately the only thing in
this repo that speaks to the platform over HTTPS with a dashboard API key.

Doctrine placement (2026-08-29 operator ruling, and the standing 2026-08-03
OMN-15680 ruling it does not disturb):

* the Claude Code plugin makes no cloud calls at all — the skill is a shim over
  the CLI;
* the ``onex`` CLI is the sole gateway client;
* this surface reaches that CLI by ENTRY POINT (``onex.cli`` -> ``cloud``), so
  installing the delegate market package is what makes ``onex cloud`` exist.
  Nothing hand-wires it; ``tests/unit/cli/
  test_cloud_cli_entry_point_registration_omn16967.py`` is the ratchet.

The internal delegation path (``onex delegate``, bus/in-process, omnimarket node
contracts) is untouched and stays bus-only. Customers never speak Kafka; their
credentials are minted without broker authorization by design.
"""

from __future__ import annotations
