# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""AC5 (Task 19, finding 1 of the capped review): no reader chooses its store backend or names a broker from its overlay.

The store check fails on a planted address, port, database, key prefix, host name or implementation kind in the
settings-store surface of omnimarket (source and contracts), and passes the tree as it stands.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import omnimarket
from omnimarket.handlers.overlay_store.store_address_check import (
    STORE_SURFACE,
    find_store_findings,
    scan_text,
)

PACKAGE = Path(omnimarket.__file__).parent


def test_no_reader_backend_choice_the_store_surface_holds_no_finding() -> None:
    findings = find_store_findings(PACKAGE)
    assert findings == [], "\n".join(str(f) for f in findings)


def test_no_reader_backend_choice_the_surface_names_files_that_exist() -> None:
    missing = [p for p in STORE_SURFACE if not (PACKAGE / p).is_file()]
    assert missing == []


@pytest.mark.parametrize(
    ("planted", "kind"),
    [
        ('HOST = "192.0.2.17"\n', "address"),
        ('URL = "valkey://cache.internal:6379/0"\n', "address"),
        ('ADDR = "cache.internal:6379"\n', "address"),
        ('client = Redis(host="localhost", port=6379)\n', "port"),
        ("port: 6379\n", "port"),
        ("database: 3\n", "database"),
        ('key_prefix = "settings:v1"\n', "key-prefix"),
        ("key_prefix: settings:v1\n", "key-prefix"),
        ('kind = "store_valkey"\n', "implementation-kind"),
        ("backend: postgres\n", "implementation-kind"),
        ("kafka_bootstrap: broker.internal:9092\n", "address"),
    ],
)
def test_no_reader_backend_choice_the_check_fails_on_a_planted_value(
    planted: str, kind: str
) -> None:
    findings = scan_text("planted.txt", planted)
    assert [f.kind for f in findings] == [kind] or kind in {f.kind for f in findings}


@pytest.mark.parametrize(
    "clean",
    [
        "key_prefix = bootstrap[PREFIX_INPUT]\n",
        "port = int(inputs[PORT_INPUT])\n",
        "kind = bootstrap.get(KIND_INPUT)\n",
        "# the store is wired from the bootstrap inputs\n",
        "backend: ${BACKEND_FROM_WIRING}\n",
    ],
)
def test_no_reader_backend_choice_positive_control_names_from_the_wiring_pass(
    clean: str,
) -> None:
    assert scan_text("clean.txt", clean) == []


def test_no_reader_backend_choice_a_planted_value_in_a_surface_file_is_found(
    tmp_path: Path,
) -> None:
    relative = STORE_SURFACE[0]
    target = tmp_path / relative
    target.parent.mkdir(parents=True)
    target.write_text('ADDRESS = "192.0.2.9"\n', encoding="utf-8")
    findings = find_store_findings(tmp_path, surface=(relative,))
    assert [f.path for f in findings] == [relative]
