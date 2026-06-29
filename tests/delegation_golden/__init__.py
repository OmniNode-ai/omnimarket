# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Delegation regression suite — versioned golden-task corpus + runner (OMN-13540).

Two layers:

* ``test_layer1_contract_unit`` — deterministic contract/unit tests that run in
  omnimarket's existing pytest CI on every PR (no live model). They catch the
  import / wiring / config regression class.
* ``runner`` + ``test_layer2_live_runner`` — load the corpus, publish each
  integration case to the live bus, read the ``delegation_events`` projection,
  and assert the ``expected`` block. Behavioral assertions only. NIGHTLY.

The corpus itself is data: ``corpus.yaml``. Adding a regression test = adding a
row.
"""
