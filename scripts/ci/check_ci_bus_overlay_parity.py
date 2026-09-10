# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Cross-repo parity for the CI bus lane overlay [OMN-18127].

THE SHAPE THIS CLOSES. ``config/ci_bus_lanes.yaml`` is written and reviewed
HERE, in omnimarket. It is validated in omnibase_infra by ``ModelCiBusOverlay``
in ``scripts/trigger_rebuild_on_merge.py``, which is ``extra="forbid"``. The
strict half of one contract therefore lives in a different repository from the
file it constrains, and until now nothing in THIS repository compared them.

So a key added here passes every check here, merges, and the failure lands
afterwards on somebody else's unrelated omnibase_infra pull request. Three
times:

  * OMN-18012 (2026-09-07) -- the transport keys; omnibase_infra run
    34160709151 red.
  * OMN-18060 / OMN-18083 (2026-09-09) -- ``projection_readback`` added by
    omnimarket#2420; ten consecutive rebuild-trigger runs red.
  * OMN-16964 / OMN-18127 (2026-09-10) -- ``ledger_readback`` added by
    omnimarket#2437 (``9d14eecf``, merged 04:17:04Z); the Runtime Rebuild
    Trigger red at 04:19:40Z and the REQUIRED ``ci-bus-overlay-binding``
    context with it.

The cost is not a red job. That publisher is what emits the rebuild-requested
event, so while it refuses the overlay the deploy agent does not rebuild the
.201 dev lane on merge -- the automatic lab pass of CLAUDE.md rule 24(a), off
for every lane, including work that has nothing to do with the overlay.

WHAT THIS IS, AND HOW IT DIFFERS FROM ITS SIBLING. omnibase_infra already runs
``ci-bus-overlay-binding``, which validates the LIVE overlay from
omnimarket@dev against its own model on every infra PR and two-hourly
(OMN-18096). That bounds how long a skew can go UNDISCOVERED, and it worked --
it went red minutes after the third key landed. What it cannot do is stop the
key landing, because by the time it runs the omnimarket PR has already merged.

This is the mirror, and it is deliberately the stronger direction: it runs on
the omnimarket PR that ADDS the key, so the failure lands on its cause.

FAIL CLOSED, EVERYWHERE. A parity gate that cannot read its counterpart has not
passed; it has not run. Every one of these is an exit-1 failure and never a
skip: the overlay is missing or unreadable, the consumer model is missing or
unreadable, the consumer module does not import, it has no
``load_ci_bus_overlay``, or -- the subtle one -- the consumer's models no
longer declare ``extra="forbid"``.

WHY THE ``extra="forbid"`` ASSERTION IS PART OF THE GATE AND NOT A SEPARATE
CONCERN. This whole check is a re-run of the consumer's own strictness. If a
future change relaxes ``ModelCiBusLane`` to ``extra="allow"``, every overlay
would validate here, this gate would report green forever, and it would be
green precisely when the strictness it proxies had been removed. That is a
worse failure than the one it exists to catch, because it is silent. So the
strictness is asserted directly against the fetched source rather than assumed
(OMN-18127 AC4).
"""

from __future__ import annotations

import argparse
import ast
import importlib.util
import sys
from pathlib import Path

#: Models in the consumer module that MUST remain ``extra="forbid"``. If one of
#: these is relaxed, this gate stops proving anything and says so loudly rather
#: than reporting a green it has not earned.
_REQUIRED_STRICT_MODELS = ("ModelCiBusOverlay", "ModelCiBusLane")

#: The consumer-side entry point this gate re-runs.
_CONSUMER_LOADER = "load_ci_bus_overlay"

#: Imported under a real ``sys.modules`` name on purpose. The consumer module
#: uses ``from __future__ import annotations`` and rebuilds its nested models
#: with an explicit types namespace; loading it by path WITHOUT registering it
#: leaves pydantic unable to resolve a nested annotation, which raises at import
#: and would read here as "the consumer is broken" rather than "we loaded it
#: wrongly". Measured on omnibase_infra#3387's own suite.
_CONSUMER_MODULE_NAME = "_onex_ci_bus_overlay_consumer_model"


class ParityCheckError(RuntimeError):
    """A refusal. Every construction of this is an exit-1 path, never a skip."""


def _read_consumer_source(consumer_model: Path) -> str:
    """Read the consumer module's source, refusing anything unreadable."""
    if not consumer_model.is_file():
        raise ParityCheckError(
            f"The consumer-side model was not checked out at {consumer_model}. "
            "This run compared nothing. Verify the omnibase_infra sparse "
            "checkout in the workflow rather than treating this as a pass."
        )
    try:
        source = consumer_model.read_text(encoding="utf-8")
    except OSError as exc:
        raise ParityCheckError(
            f"The consumer-side model at {consumer_model} could not be read: {exc}"
        ) from exc
    if not source.strip():
        raise ParityCheckError(
            f"The consumer-side model at {consumer_model} is empty. A silently "
            "empty sparse checkout is the exact shape this gate must refuse."
        )
    return source


def assert_consumer_is_strict(source: str, *, consumer_ref: str) -> None:
    """Refuse a consumer whose lane models no longer forbid unknown keys.

    Read from the SOURCE with ``ast`` rather than from the imported class, so
    the assertion cannot be satisfied by a runtime mutation and does not depend
    on the module importing at all.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        raise ParityCheckError(
            f"The consumer-side model at {consumer_ref} does not parse: {exc}"
        ) from exc

    strict_models: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef):
            continue
        if node.name not in _REQUIRED_STRICT_MODELS:
            continue
        for statement in node.body:
            if not isinstance(statement, ast.Assign):
                continue
            targets = [t.id for t in statement.targets if isinstance(t, ast.Name)]
            if "model_config" not in targets:
                continue
            call = statement.value
            if not isinstance(call, ast.Call):
                continue
            for keyword in call.keywords:
                if (
                    keyword.arg == "extra"
                    and isinstance(keyword.value, ast.Constant)
                    and keyword.value.value == "forbid"
                ):
                    strict_models.add(node.name)

    missing = sorted(set(_REQUIRED_STRICT_MODELS) - strict_models)
    if missing:
        raise ParityCheckError(
            f"The consumer-side models {missing} at {consumer_ref} no longer "
            'declare ConfigDict(extra="forbid"). This gate re-runs that '
            "strictness, so with it removed every overlay would validate here "
            "and this check would report green exactly when the thing it "
            "proxies had been deleted. Refusing rather than passing. If the "
            "relaxation is intended, this gate has no purpose and should be "
            "removed deliberately, not left reporting a green it cannot earn."
        )


def _load_consumer_loader(consumer_model: Path):  # type: ignore[no-untyped-def]
    """Import the consumer module by path and return its overlay loader."""
    spec = importlib.util.spec_from_file_location(_CONSUMER_MODULE_NAME, consumer_model)
    if spec is None or spec.loader is None:
        raise ParityCheckError(
            f"The consumer-side model at {consumer_model} could not be loaded "
            "as a Python module."
        )
    module = importlib.util.module_from_spec(spec)
    # Registered BEFORE exec so pydantic can resolve postponed annotations.
    sys.modules[_CONSUMER_MODULE_NAME] = module
    try:
        spec.loader.exec_module(module)
    except Exception as exc:
        raise ParityCheckError(
            f"The consumer-side model at {consumer_model} failed to import: "
            f"{type(exc).__name__}: {exc}. The overlay was NOT validated."
        ) from exc
    finally:
        sys.modules.pop(_CONSUMER_MODULE_NAME, None)

    loader = getattr(module, _CONSUMER_LOADER, None)
    if loader is None or not callable(loader):
        raise ParityCheckError(
            f"The consumer-side model at {consumer_model} has no callable "
            f"{_CONSUMER_LOADER!r}. Its contract surface moved; this gate must "
            "be repointed rather than left reporting green."
        )
    return loader


def check_overlay_parity(
    *,
    overlay: Path,
    consumer_model: Path,
    consumer_ref: str,
) -> None:
    """Validate this repository's overlay against the consumer's live model.

    Raises :class:`ParityCheckError` on every failure mode. Returns ``None`` on
    the single success path.
    """
    if not overlay.is_file():
        raise ParityCheckError(
            f"The overlay was not found at {overlay}. This run compared nothing."
        )

    source = _read_consumer_source(consumer_model)
    assert_consumer_is_strict(source, consumer_ref=consumer_ref)
    loader = _load_consumer_loader(consumer_model)

    try:
        loader(overlay)
    except Exception as exc:
        raise ParityCheckError(
            f"{overlay} is REFUSED by the omnibase_infra publisher's model at "
            f"{consumer_ref}:\n\n{exc}\n\n"
            "This is the parity failure this gate exists to move onto the pull "
            "request that causes it. The fix is to teach the consumer-side "
            "model the key -- land the omnibase_infra change first, merge it, "
            'and this check goes green. Do NOT relax extra="forbid" there: an '
            "unknown key in this overlay is indistinguishable from a typo that "
            "would route a publisher to a lane it never declared, which is the "
            "OMN-14800 silent misroute the strictness exists to refuse."
        ) from exc


def main(argv: list[str] | None = None) -> int:
    """CLI entry point. Exit 0 only when the overlay validated."""
    parser = argparse.ArgumentParser(
        description=(
            "Validate this repository's CI bus lane overlay against the "
            "omnibase_infra publisher's model, resolved live at check time."
        )
    )
    parser.add_argument(
        "--overlay",
        type=Path,
        default=Path("config/ci_bus_lanes.yaml"),
        help="Path to this repository's ci_bus_lanes.yaml",
    )
    parser.add_argument(
        "--consumer-model",
        type=Path,
        required=True,
        help=(
            "Path to the checked-out omnibase_infra scripts/trigger_rebuild_on_merge.py"
        ),
    )
    parser.add_argument(
        "--consumer-ref",
        default="omnibase_infra@dev",
        help="Human-readable identity of the consumer side, for the message",
    )
    args = parser.parse_args(argv)

    try:
        check_overlay_parity(
            overlay=args.overlay,
            consumer_model=args.consumer_model,
            consumer_ref=args.consumer_ref,
        )
    except ParityCheckError as exc:
        print(f"::error::CI bus overlay parity FAILED\n{exc}", file=sys.stderr)
        return 1

    print(
        f"OK: {args.overlay} validates against the publisher model at "
        f"{args.consumer_ref}."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
