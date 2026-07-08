# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""2x2 SWE-discriminator harness (OMN-13988) — SPIKE / de-risk scaffold.

The 2x2 factorial is {monolith, decomposed} x {frontier-only, cost-routed}.
This package extends the graded_ladder benchmark (OMN-13935/13938) from
toy-puzzle / model-tier-only scoring to repo-grounded SWE tasks replayed from
held-back merged PRs, run under each arm, and graded blind and offline over
captured artifacts.

proof_class = "offline over captured artifacts" — NOT a closed live runtime
loop. The runner never grades (verifier != runner): run_smoke captures
artifacts, grader.py scores them in a separate pass.
"""
