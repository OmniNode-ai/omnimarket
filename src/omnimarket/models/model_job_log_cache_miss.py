# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Markers the gateway's job-log refusals start with; the filler reads the miss marker."""

#: What a cache-only read's refusal starts with; the filler reads it to tell a miss from a fault.
JOB_LOG_CACHE_MISS = "JOB-LOG-CACHE-MISS"

#: What a refusal starts with when the job's per-job lock is not free inside the declared wait.
JOB_LOG_BUSY = "JOB-LOG-BUSY"

#: What a refusal starts with when a log download outlasts the declared deadline.
JOB_LOG_FILL_TIMEOUT = "JOB-LOG-FILL-TIMEOUT"
