"""Shared seam-projection primitives (OMN-15763).

Domain module reused by ``node_seam_graph_compute`` and
``node_seam_match_compute``: the ``ModelSeamProjection`` wire-crossing shape
and its canonical (sorted-key, schema-versioned) serialization + sha256
pinning. Both nodes import from here rather than duplicating the projection
shape or the hashing idiom.
"""

from __future__ import annotations

__all__: list[str] = []
