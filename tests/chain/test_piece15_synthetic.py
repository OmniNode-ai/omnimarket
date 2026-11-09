# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Piece 15 synthetic delegated task tests, at the path the ticket's falsifiers name (OMN-22977).

The tests are written once, in ``tests/chains/test_piece15_synthetic.py``, beside the chain
tests; the contracts that already name that file keep passing. This module
collects the same tests under ``tests/chain/`` so ``pytest tests/chain/test_piece15_synthetic.py`` and
its ``-k drop_result_flips`` selection run them.
"""

from __future__ import annotations

from tests.chains import test_piece15_synthetic as _piece15_chain

globals().update(
    {
        name: value
        for name, value in vars(_piece15_chain).items()
        if not name.startswith("__")
    }
)
