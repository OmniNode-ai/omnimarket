# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""CLI of the overlay root resolver (OMN-24032).

A golden run (roots in store, clone, declared order, exit 0, JSON parses), an error chain
(no root present: exit 2, finding on err, JSON roots empty), a repeatable --declared-root
order test, and a failing control that the declared roots are not reordered.
"""

from __future__ import annotations

import io
import json

import pytest

from omnimarket.nodes.node_overlay_roots_resolve_effect.runtime_overlay_roots_resolve import (
    main,
)

pytestmark = pytest.mark.unit


class FakeProbe:
    def __init__(self, dirs: set[str], files: dict[str, str]) -> None:
        self._dirs = dirs
        self._files = files

    def is_dir(self, path: str) -> bool:
        return path in self._dirs

    def read_text(self, path: str) -> str | None:
        return self._files.get(path)

    def resolve(self, path: str) -> str:
        return path


def _run(argv: list[str], probe: FakeProbe) -> tuple[int, dict, str]:
    out = io.StringIO()
    err = io.StringIO()
    code = main(argv, probe=probe, out=out, err=err)
    payload = json.loads(out.getvalue())
    return code, payload, err.getvalue()


def test_a_golden_run_prints_the_ordered_roots_and_exits_zero() -> None:
    registry = "/opt/omni/registry"
    clone = "/opt/omni/overlays/overlays"
    store = "/srv/fixture/u/.local/state/onex/overlay-store"
    declared = "/opt/omni/plugins/overlays"
    second_clone = "/opt/omni/overlay-store-reader/overlays/overlays"
    probe = FakeProbe(
        dirs={clone, second_clone, store, declared},
        files={
            f"{clone}/node_overlay_store_read_effect/overlay.yaml": (
                "snapshot_root: ~/.local/state/onex/overlay-store\n"
            ),
        },
    )
    code, payload, _ = _run(
        [
            "--registry-root",
            registry,
            "--home",
            "/srv/fixture/u",
            "--declared-root",
            declared,
        ],
        probe,
    )
    assert code == 0
    assert payload["roots"] == [store, clone, second_clone, declared]
    assert [c["source"] for c in payload["considered"] if c["present"]] == [
        "store",
        "canonical_clone",
        "canonical_clone",
        "declared",
    ]
    assert payload["finding"] is None


def test_no_root_present_exits_two_with_a_finding_on_err() -> None:
    registry = "/opt/omni/registry"
    clone = "/opt/omni/overlays/overlays"
    probe = FakeProbe(dirs=set(), files={})
    code, payload, err = _run(
        [
            "--registry-root",
            registry,
            "--home",
            "/srv/fixture/u",
            "--declared-root",
            "/opt/omni/plugins",
        ],
        probe,
    )
    assert code == 2
    assert payload["roots"] == []
    assert payload["finding"] is not None
    assert clone in err


def test_a_repeatable_declared_root_keeps_its_order() -> None:
    registry = "/opt/omni/registry"
    clone = "/opt/omni/overlays/overlays"
    store = "/srv/fixture/u/.local/state/onex/overlay-store"
    first = "/opt/omni/declared/first"
    second = "/opt/omni/declared/second"
    probe = FakeProbe(
        dirs={clone, store, first, second},
        files={
            f"{clone}/node_overlay_store_read_effect/overlay.yaml": (
                "snapshot_root: ~/.local/state/onex/overlay-store\n"
            ),
        },
    )
    code, payload, _ = _run(
        [
            "--registry-root",
            registry,
            "--home",
            "/srv/fixture/u",
            "--declared-root",
            first,
            "--declared-root",
            second,
        ],
        probe,
    )
    assert code == 0
    assert payload["roots"] == [store, clone, first, second]


def test_the_declared_roots_are_not_reordered() -> None:
    registry = "/opt/omni/registry"
    clone = "/opt/omni/overlays/overlays"
    store = "/srv/fixture/u/.local/state/onex/overlay-store"
    first = "/opt/omni/declared/first"
    second = "/opt/omni/declared/second"
    probe = FakeProbe(
        dirs={clone, store, first, second},
        files={
            f"{clone}/node_overlay_store_read_effect/overlay.yaml": (
                "snapshot_root: ~/.local/state/onex/overlay-store\n"
            ),
        },
    )
    _, payload, _ = _run(
        [
            "--registry-root",
            registry,
            "--home",
            "/srv/fixture/u",
            "--declared-root",
            first,
            "--declared-root",
            second,
        ],
        probe,
    )
    declared = [
        root for root in payload["roots"] if root.startswith("/opt/omni/declared/")
    ]
    assert declared == [first, second]
