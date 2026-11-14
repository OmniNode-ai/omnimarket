# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The host disk orchestrator node (OMN-23866).

Runs on its contract schedule and on a low-disk event: reads the newest disk reading per host, asks
node_host_disk_decision_compute what each host evicts and whether it alarms, sends one typed command per decision to
the host disk effect, collects each typed result or failure, and records the run.
"""
