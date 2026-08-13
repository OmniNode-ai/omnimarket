# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Milestone-B traversed-slice real-seam goldens (OMN-16004).

Executable producer -> registry -> consumer goldens for exactly the seam
edges the Milestone-B activation receipt traverses, plus the WS-7 mandatory
high-severity union. The frozen slice enumeration lives in
``slice_manifest.yaml`` (a committed, inspectable artifact the tests load);
``manifest.py`` is its typed loader and ``harness.py`` holds the shared
real-seam driving machinery.

Scope bound: this package is the narrow pre-activation-exempt slice. The
full 15-edge golden program is a separate, still-blocked ticket and is NOT
claimed complete here — ``test_manifest_registry_binding.py`` asserts that
bound structurally (excluded edges must carry an exclusion reason and must
have no golden module).
"""
