# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Print the merged delegation routing with a per-key source (OMN-18670).

    python -m omnimarket.cli.cli_explain_routing
    python -m omnimarket.cli.cli_explain_routing --fail-on-shadow

The delegation routing config an `onex delegate` run actually resolves is not
any one file. It is the committed contract
(``src/omnimarket/configs/bifrost_delegation.yaml``) deep-merged field-by-field
with whichever overlay is active — the store blob under
``delegation.bifrost.overlay`` in a deployment, or the machine-local file
``~/.omninode/delegation/bifrost_overrides.yaml`` on a standalone install. Once
merged, the two authorities are one dict, and nothing downstream can say which
supplied any given value.

This command prints the merge with that distinction intact: one line per
resolved backend field, naming the authority and the artifact that supplied it,
and calling out every place the overlay wrote over a field the committed
contract had already declared.

It reads the same bindings the routing path reads, so what it prints is what
delegation resolves — and it reads only, mutating nothing. Run it when a
delegation probe disagrees with what every repository surface says, BEFORE
touching any repository: on 2026-09-18 that disagreement cost three lanes a
hand re-derivation, and the answer was one line in a file in ``$HOME``.

Invoked with ``python -m`` rather than a console script deliberately: a console
script only appears after the package is reinstalled, and the host where you
need this answer is precisely the host whose installed build you are doubting.
``python -m`` runs against whatever is importable right now, which is the thing
under suspicion.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

from omnimarket.models.delegation.model_bifrost_overlay_provenance import (
    AUTHORITATIVE_BACKEND_FIELDS,
    ModelBifrostOverlayProvenance,
)
from omnimarket.routing.delegation_backend_resolution import (
    load_bifrost_backends_with_provenance,
)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m omnimarket.cli.cli_explain_routing",
        description=(
            "Print the merged bifrost delegation routing with a per-key "
            "source, and flag every overlay override of a field the committed "
            "contract declares (OMN-18670)."
        ),
    )
    # Both default to None so the routing authority supplies its OWN defaults.
    # Restating them here would give this command a second copy of the default
    # bindings that could drift from the ones delegation actually resolves,
    # which would make the explain surface a confident liar.
    parser.add_argument(
        "--contract-path",
        type=Path,
        default=None,
        help="Committed routing contract to read (default: the packaged one).",
    )
    parser.add_argument(
        "--overlay-path",
        type=Path,
        default=None,
        help=(
            "Site overlay file to merge (default: "
            "~/.omninode/delegation/bifrost_overrides.yaml). A path that does "
            "not exist is reported as 'no overlay merged', not an error."
        ),
    )
    parser.add_argument(
        "--fail-on-shadow",
        action="store_true",
        help=(
            "Exit 1 when the overlay writes over a field the committed "
            "contract declares, so this command can be used as a probe."
        ),
    )
    return parser


def _render_report(provenance: ModelBifrostOverlayProvenance) -> str:
    """Render the per-key source report. Pure — takes a record, returns text."""
    lines: list[str] = [
        "merged delegation routing — per-key source (OMN-18670)",
        f"  committed contract : {provenance.contract_source}",
        f"  active overlay     : {provenance.overlay_source or '(none merged)'}",
        "",
    ]

    by_backend: dict[str, list[str]] = {}
    for field in provenance.fields:
        rendered = "null" if field.value is None else field.value
        marker = "  !! " if field.shadows_authoritative_field else "     "
        # Name the AUTHORITY KIND as well as the artifact. A bare path answers
        # "which file" but not "was this the contract or an override", and the
        # second question is the one an operator is actually asking when the
        # merged value disagrees with the repository.
        source = f"{field.source.value} {field.source_ref}"
        line = f"{marker}{field.field_name:<22} = {rendered!s:<48} <- {source}"
        if field.shadowed_value is not None:
            # SHADOWS is reserved for a field the committed contract OWNS —
            # the same set ``--fail-on-shadow`` counts. An overlay write over
            # any other declared field is an ordinary override and says so, so
            # the two cannot be confused by someone reading this output.
            verb = "SHADOWS" if field.shadows_authoritative_field else "overrides"
            line += (
                f"  [{verb} {field.shadowed_value!r} from {field.shadowed_source_ref}]"
            )
        by_backend.setdefault(field.backend_id, []).append(line)

    for backend_id, entries in by_backend.items():
        lines.append(f"backends[{backend_id}]")
        lines.extend(entries)
        lines.append("")

    shadows = provenance.shadows()
    if not shadows:
        lines.append(
            "No overlay override of a committed authoritative field "
            f"({', '.join(sorted(AUTHORITATIVE_BACKEND_FIELDS))}). The overlay "
            "is acting as the bootstrap fallback it is documented to be."
        )
        return "\n".join(lines)

    lines.append(
        f"{len(shadows)} overlay override(s) of a field the committed contract "
        "declares:"
    )
    for shadow in shadows:
        lines.append(f"  - {shadow.describe()}")
    lines.append(
        "\nThe committed contract owns these fields. Remove the key from the "
        "overlay rather than repointing it — an overlay pin that agrees with "
        "the contract today silently reverts the next repoint on this host, "
        "which is how the 2026-09-18 delegation outage happened "
        "(OMN-18670; retire the overlay precedence path: OMN-17989)."
    )
    return "\n".join(lines)


def _resolve(
    contract_path: Path | None, overlay_path: Path | None
) -> ModelBifrostOverlayProvenance:
    """Call the routing authority, letting IT supply any default not overridden.

    Spelled as four explicit calls rather than a kwargs dict so the defaults
    stay the callee's to declare: this command must never carry its own copy of
    the default contract/overlay bindings, or it could confidently explain a
    merge that delegation does not actually perform.
    """
    if contract_path is None and overlay_path is None:
        return load_bifrost_backends_with_provenance()[1]
    if contract_path is None:
        assert overlay_path is not None
        return load_bifrost_backends_with_provenance(overlay_path=overlay_path)[1]
    if overlay_path is None:
        return load_bifrost_backends_with_provenance(config_path=contract_path)[1]
    return load_bifrost_backends_with_provenance(
        config_path=contract_path, overlay_path=overlay_path
    )[1]


def main(argv: Sequence[str] | None = None) -> int:
    """Print the merged routing with a per-key source. Returns the exit code."""
    args = _build_parser().parse_args(argv)

    provenance = _resolve(args.contract_path, args.overlay_path)
    # ``sys.stdout.write`` rather than ``print``: this repo bans bare prints in
    # ``src/`` (ruff T20), and this module is an argparse ``python -m``
    # entrypoint rather than a click command, so ``click.echo`` would import a
    # CLI framework for one line of output.
    sys.stdout.write(_render_report(provenance) + "\n")

    if args.fail_on_shadow and provenance.shadows():
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover - module entrypoint
    sys.exit(main())
