# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Digest helper for node_host_install_readback_compute (OMN-23957).

``digest_text`` returns the lowercase hex digest of ``text`` encoded utf-8 using ``hashlib.new(algorithm)``.
Raises ``ValueError`` naming the algorithm if it is not in ``hashlib.algorithms_guaranteed``.
"""

from __future__ import annotations

import hashlib


def digest_text(text: str, algorithm: str) -> str:
    """Return the lowercase hex digest of ``text`` using the named algorithm.

    Raises ``ValueError`` if ``algorithm`` is not in ``hashlib.algorithms_guaranteed``.
    """
    if algorithm not in hashlib.algorithms_guaranteed:
        raise ValueError(f"unknown digest algorithm: {algorithm!r}")
    return hashlib.new(algorithm, text.encode("utf-8")).hexdigest().lower()
