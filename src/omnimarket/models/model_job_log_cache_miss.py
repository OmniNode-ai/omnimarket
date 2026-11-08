# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Marker shared by the gateway's cache-only refusal and the job-log filler."""

#: What a cache-only read's refusal starts with; the filler reads it to tell a miss from a fault.
JOB_LOG_CACHE_MISS = "JOB-LOG-CACHE-MISS"
