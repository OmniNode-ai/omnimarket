# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Fictional developer.principal overlay documents; no real host or person."""

from __future__ import annotations

from typing import Any


def machine(principal: str, *roles: str) -> dict[str, Any]:
    return {
        "principal": principal,
        "login": f"{principal}-svc",
        "device": principal,
        "role": "machine",
        "grant_roles": list(roles),
    }


def developer(principal: str) -> dict[str, Any]:
    return {"principal": principal, "login": f"{principal}-dev", "device": principal}


def overlay(*rows: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": "developer_principal.v1",
        "grants_source": "deployment-profile",
        "principals": list(rows),
    }


TWO_WRITERS = overlay(
    machine("lab-host-a", "scm-writer"),
    machine("lab-host-b", "scm-writer"),
    machine("lab-host-c"),
    developer("workstation-a"),
)
