# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The paired route of the overlay required check (OMN-24052): judge an OPEN overlay-repo pull request that names this one as its pair.

A node's overlay is read from the overlay repository's main, and that repository's CI loads a pinned overlay through the node's
model at the pinned commit, so a new node and its first overlay cannot merge in either order. The pair route breaks the loop
without dropping either guarantee: the overlay is accepted from the open pull request only when

- the CI step fetched the pull request's head to ``refs/pair-overlay/<n>`` in the overlay checkout (a number that was not fetched is refused);
- that head holds ``<documents dir>/<id>/overlay.yaml`` and a ``.omnimarket-node-pins`` line for ``<id>`` pinned at THIS pull
  request's head; and
- the document loads through the model class of THIS tree (the same ``validate_entry`` the load check uses), so the load guarantee holds.

The merged-pin guarantee is the other repository's: its pin check fails the pull request until the pin is on omnimarket dev.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from omnibase_core.validators.no_unguarded_git_subprocess import (
    scrub_git_location_env,
)

from omnimarket.nodes.node_overlay_drift_effect.adapters.overlay_home import (
    parse_pins,
)
from omnimarket.nodes.node_overlay_drift_effect.protocols import OverlayHomeError
from omnimarket.nodes.node_overlay_drift_effect.validate_program import (
    validate_entry,
)
from omnimarket.nodes.node_overlay_main_load_check_effect.handlers.model_resolver import (
    ModelImportFailedError,
    ModelUnresolvedError,
    default_search_roots,
    resolve_spec,
)

PAIR_REF_PREFIX = "refs/pair-overlay"
PINS_FILE_NAME = ".omnimarket-node-pins"


def _show(checkout: Path, ref: str, path: str) -> str | None:
    done = subprocess.run(
        ["git", "show", f"{ref}:{path}"],
        cwd=checkout,
        check=False,
        capture_output=True,
        text=True,
        env=scrub_git_location_env(os.environ),
    )
    return done.stdout if done.returncode == 0 else None


def open_pair_refusal(
    checkout: Path, documents_dir: str, pr: int, overlay_id: str, head_sha: str
) -> str | None:
    """Why the open pull request ``pr`` does not satisfy ``overlay_id``, or ``None`` when it does."""
    ref = f"{PAIR_REF_PREFIX}/{pr}"
    pins_text = _show(checkout, ref, PINS_FILE_NAME)
    if pins_text is None:
        return f"overlay-repo PR {pr} was not fetched into the checkout ({ref}), so it was not read"
    document = _show(checkout, ref, f"{documents_dir}/{overlay_id}/overlay.yaml")
    if document is None:
        return (
            f"overlay-repo PR {pr} holds no {documents_dir}/{overlay_id}/overlay.yaml"
        )
    try:
        pins = [p for p in parse_pins(pins_text) if p.overlay_id == overlay_id]
    except OverlayHomeError as exc:
        return f"overlay-repo PR {pr} has a malformed pins file: {exc}"
    if not pins:
        return f"overlay-repo PR {pr} has no {PINS_FILE_NAME} line for {overlay_id}"
    pin = pins[-1]
    if pin.revision != head_sha:
        return (
            f"overlay-repo PR {pr} pins {overlay_id} at {pin.revision[:12]}, not at this pull request's head "
            f"{head_sha[:12]}, so it does not name this pull request as its pair"
        )
    try:
        spec = resolve_spec(pin.model, default_search_roots())
    except ModelUnresolvedError:
        return f"the model {pin.model} exists nowhere in this tree, so no code reads the document"
    except ModelImportFailedError as exc:
        return f"the model {pin.model} did not import: {exc}"
    verdict = validate_entry(
        {"overlay_id": overlay_id, "model": spec, "text": document}
    )
    if verdict["verdict"] != "accepted":
        fields = ", ".join(verdict["fields"]) or verdict["error_class"]
        return f"this tree's {spec.partition(':')[2]} refused the document of overlay-repo PR {pr}; fields: {fields}"
    return None
