#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Verification-hold CI gate (OMN-15483 criterion 3, controller half).

Why a CI job and not more node code
-----------------------------------
The first OMN-15483 round taught the merge *node*
(``node_pr_lifecycle_merge_effect``) to honor the hold marker. That bound one
consumer. It did not bind the consumer that actually performed every merge in
the ticket's incident table: the **foreground Codex merge controller**, which is
a session driving ``gh pr merge`` and contains no omnimarket code at all. The
ticket says so explicitly:

    Note the controller here is currently a foreground Codex sweep, not only
    ``node_pr_lifecycle_orchestrator``. The hold must bind **both** consumers,
    or it is trivially bypassed by whichever one is driving.

Chasing consumers does not converge — the next one (a human running
``gh pr merge``, repo auto-merge, a merge queue, a future node) starts unbound
again. So this gate does not live in any consumer. It lives at the single choke
point **every** consumer already respects: the PR's required status checks.

A held PR fails this job. This job is registered in the fail-closed ``CI Summary``
strict set (``scripts/ci/ci_summary_gate.py``), which is a required
branch-protection context. Therefore a held PR can never be required-green, and
every consumer — Codex controller, ``gh pr merge``, auto-merge, merge queue, and
the node path — refuses it without containing one line of hold-specific code.

One vocabulary — enforced by construction, not by convention
------------------------------------------------------------
This script declares **no regex**. It loads
``src/omnimarket/merge_control/hold_marker.py`` by file path and calls the
canonical :func:`evaluate_merge_hold` out of it, so the CI verdict and the node
verdict are computed by the same function over the same vocabulary. There is no
vendored copy to drift and no sync test to forget to run: mutating
``HOLD_MARKER_RE`` changes this gate's behavior in the same commit, and deleting
the canonical module makes this gate FAIL rather than silently pass.

The load is by path (``importlib.util.spec_from_file_location``) rather than
``from omnimarket.merge_control import ...`` on purpose: it skips the package
``__init__``, so the gate needs no ``uv sync`` and no third-party dependency. The
canonical module is stdlib-only, so a bare ``python3`` on a fresh checkout runs
it in about a second. That matters — this job must be able to render a verdict
even when the heavy CI lane is not running (see "unconditional" below).

One URL authority — the same rule as the vocabulary
---------------------------------------------------
omnimarket declares exactly one authority for every external base URL:
``src/omnimarket/configs/service_endpoints.yaml`` (OMN-12806). Its own header
says no module may hardcode those strings, and ``github_api.py`` plus every node
handler that talks to GitHub reads ``github.rest_url`` from it through the typed
accessor ``omnimarket.config.service_endpoints.GITHUB_REST_URL``. The
``URL Authority Gate`` enforces that repo-wide.

This gate reads the same authority *file* rather than importing that accessor,
because the accessor is PyYAML-backed and this job runs a bare ``python3`` with
no ``uv sync`` (see the vocabulary section above for why that property is
load-bearing) — and PyYAML is not in the hosted runner image's system Python.
So :func:`resolve_github_rest_url` reads the one authority with a strict scalar
reader over the exact ``github: rest_url:`` path. The *value* still lives in
exactly one place in the repo; only the parse is separate, and
``test_gate_resolves_the_same_url_as_the_typed_accessor`` asserts the two
readers return an identical string, so any divergence is RED. If the authority
cannot be read, the gate FAILS rather than falling back to a literal — the same
fail-closed posture as a missing vocabulary.

Which surfaces are read, and in what order
------------------------------------------
1. **Live PR state** (``{github.rest_url}/repos/{repo}/pulls/{number}``), used
   when the fetch succeeds. Preferred because it is *current*: setting or
   clearing a hold and re-running this job takes effect immediately, which is
   what makes acceptance criterion 4 ("clearing the hold releases the PR") true
   on the CI path as well as the node path. The base URL is not a literal and
   not an env read — see "One URL authority" below.
2. **The event payload** (``PR_TITLE`` / ``PR_LABELS_JSON``, injected from the
   ``github`` context by the workflow), used when the live fetch is unavailable
   or fails. No token, no network, no API dependency for the gate to function.

Live wins when readable; the payload is the degraded fallback, never a veto. If
**neither** surface yields a title or a label set, the decision is
``INDETERMINATE`` and the gate FAILS — an unreadable hold state is treated as
held, exactly as on the node path (criterion 2). A probe that cannot see the
marker is the blindness this ticket exists to close, so it must never be scored
as "clear".

Unconditional, and why it has no ``needs:``
--------------------------------------------
The job carries no ``needs:`` and no ``if:`` in ``ci.yml``. That is deliberate:
on ``a56e3819`` the ``occ-preflight`` dependency failed and cascade-skipped
``Tests``, ``typecheck``, ``Contract Compliance Check`` and the whole E2E lane.
A hold gate that can be cascade-skipped by an unrelated upstream failure is not
a gate. Being ``needs``-free also means it renders a verdict in seconds on a
GitHub-hosted runner with zero LAN/self-hosted dependency.

Non-PR events
-------------
"Held" is a predicate over a pull request. On ``push`` and ``merge_group`` there
is no PR to evaluate, so the gate exits 0 with an explicit notice rather than
inventing a verdict. This is not a bypass: code reaches ``main`` through a PR,
and that PR was gated here. The shape mirrors the existing ``occ-preflight``
strict gate, which likewise short-circuits to 0 on non-PR events so that it is
always *present and completed* for the ``CI Summary`` completeness anchor.

Exit codes
----------
``0`` — clear (or not applicable). ``1`` — held, indeterminate, or the canonical
vocabulary could not be loaded. There is no third outcome; the gate never exits
non-deterministically.

Related:
    - OMN-15483: this ticket — the merge sweep lands PRs inside the adversarial
      verification window.
    - OMN-14741 F-17: the marker vocabulary this extends.
    - OMN-14230: the "Freeze rule", written as prose and never mechanized.
    - OMN-15214/OMN-15427: the strict-slot precedent this registration follows.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import sys
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Any

# scripts/ci/check_pr_hold_marker.py -> repo root is parents[2].
REPO_ROOT = Path(__file__).resolve().parents[2]

# THE vocabulary. Not a copy of it, not a pattern equal to it — the module
# itself. See the "One vocabulary" section above.
CANONICAL_HOLD_MODULE = (
    REPO_ROOT / "src" / "omnimarket" / "merge_control" / "hold_marker.py"
)

# THE external-endpoint authority (OMN-12806). Same rule as the vocabulary: this
# gate reads the shipped authority, it does not carry a URL of its own.
URL_AUTHORITY_FILE = (
    REPO_ROOT / "src" / "omnimarket" / "configs" / "service_endpoints.yaml"
)
URL_AUTHORITY_SECTION = "github"
URL_AUTHORITY_KEY = "rest_url"

EXIT_CLEAR = 0
EXIT_HELD = 1

# Events that carry a pull request to evaluate.
PR_EVENTS: frozenset[str] = frozenset({"pull_request", "pull_request_target"})

_API_TIMEOUT_SECONDS = 10


class CanonicalVocabularyUnavailableError(RuntimeError):
    """The canonical hold vocabulary could not be loaded.

    Fail-closed: a gate that cannot load the vocabulary must not report
    "clear". Deleting or breaking ``merge_control/hold_marker.py`` turns this
    gate RED rather than making it vacuously green.
    """


class UrlAuthorityUnavailableError(RuntimeError):
    """The external-endpoint authority could not supply the GitHub REST base URL.

    Fail-closed for the same reason as the vocabulary error: the alternative is
    a hardcoded literal, which is precisely the drift the ``URL Authority Gate``
    exists to prevent. The gate refuses rather than inventing a host.
    """


def _scalar_value(raw_value: str, authority_path: Path) -> str:
    """Extract one YAML scalar, honouring quoting and trailing comments.

    Narrow by design — it handles exactly the shapes the authority file uses and
    REFUSES anything else, because a lenient parse here would return a corrupted
    host instead of raising. ``value.strip().strip("\\"'")`` alone is not enough:
    a quoted URL followed by an inline ``#`` comment survives both the quote
    strip and the scheme-prefix check, and yields a value with a stray quote and
    the comment text still glued to the host. Caught in review on this PR and
    pinned by ``test_trailing_comments_are_stripped_not_absorbed``.

    Rules applied:

    - A quoted scalar ends at its matching closing quote. A ``#`` *inside* the
      quotes is part of the value (e.g. a URL fragment), not a comment.
    - After the closing quote only whitespace, or whitespace then a ``#``
      comment, may follow. Trailing junk is an error, not something to ignore.
    - An unquoted scalar ends at the first whitespace-preceded ``#``, matching
      YAML's comment rule (``a#b`` is a single token, ``a #b`` is not).

    Args:
        raw_value: Everything after the ``key:`` separator, unstripped.
        authority_path: Only used to build a locating error message.

    Returns:
        The scalar value with quotes and any trailing comment removed.

    Raises:
        UrlAuthorityUnavailableError: On an unterminated quote or trailing junk.
    """
    value = raw_value.strip()
    if value[:1] in {'"', "'"}:
        quote = value[0]
        closing = value.find(quote, 1)
        if closing == -1:
            raise UrlAuthorityUnavailableError(
                f"{authority_path}: {URL_AUTHORITY_SECTION}.{URL_AUTHORITY_KEY} "
                f"has an unterminated {quote} quote ({raw_value.strip()!r})"
            )
        remainder = value[closing + 1 :].strip()
        if remainder and not remainder.startswith("#"):
            raise UrlAuthorityUnavailableError(
                f"{authority_path}: {URL_AUTHORITY_SECTION}.{URL_AUTHORITY_KEY} "
                f"has unexpected trailing content after the quoted value "
                f"({remainder!r})"
            )
        return value[1:closing]

    # Unquoted: a comment must be preceded by whitespace to start one.
    for index in range(1, len(value)):
        if value[index] == "#" and value[index - 1].isspace():
            return value[:index].strip()
    return value


def resolve_github_rest_url(
    authority_path: Path = URL_AUTHORITY_FILE,
) -> str:
    """Resolve the GitHub REST base URL from the repo's endpoint authority.

    Reads ``github.rest_url`` out of ``configs/service_endpoints.yaml`` — the
    single authority every other GitHub caller in this repo resolves through
    (``omnimarket.config.service_endpoints.GITHUB_REST_URL``). A strict scalar
    reader is used instead of that typed accessor because the accessor imports
    PyYAML and this gate must render a verdict from a bare ``python3``; see the
    "One URL authority" section of the module docstring.

    The reader is deliberately narrow: a top-level ``github:`` block, one
    indented ``rest_url:`` scalar, dequoted by :func:`_scalar_value` (which also
    strips a trailing comment and refuses trailing junk). Anything else —
    missing file, missing section, missing key, empty value, an unterminated
    quote, a value that is not an absolute URL — is an error, never a default.

    Args:
        authority_path: Path to ``configs/service_endpoints.yaml``.

    Returns:
        The GitHub REST base URL exactly as the authority declares it.

    Raises:
        UrlAuthorityUnavailableError: If the authority cannot supply the value.
    """
    if not authority_path.is_file():
        raise UrlAuthorityUnavailableError(
            f"endpoint authority not found at {authority_path} — the gate "
            "refuses rather than hardcoding a GitHub host"
        )
    try:
        text = authority_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise UrlAuthorityUnavailableError(
            f"endpoint authority at {authority_path} is unreadable: {exc}"
        ) from exc

    in_section = False
    for raw_line in text.splitlines():
        if not raw_line.strip() or raw_line.lstrip().startswith("#"):
            continue
        if not raw_line[:1].isspace():
            # A top-level key: we are inside the target block only while this
            # is the one we want.
            in_section = raw_line.split(":", 1)[0].strip() == URL_AUTHORITY_SECTION
            continue
        if not in_section:
            continue
        name, separator, value = raw_line.strip().partition(":")
        if not separator or name.strip() != URL_AUTHORITY_KEY:
            continue
        url = _scalar_value(value, authority_path)
        if not url.startswith("https://") or len(url) <= len("https://"):
            raise UrlAuthorityUnavailableError(
                f"{authority_path}: {URL_AUTHORITY_SECTION}.{URL_AUTHORITY_KEY} "
                f"is not an absolute https URL ({url!r})"
            )
        return url

    raise UrlAuthorityUnavailableError(
        f"{authority_path} declares no "
        f"{URL_AUTHORITY_SECTION}.{URL_AUTHORITY_KEY} — the gate refuses rather "
        "than hardcoding a GitHub host"
    )


def load_canonical_hold_module(
    module_path: Path = CANONICAL_HOLD_MODULE,
) -> ModuleType:
    """Load the canonical hold-marker module by file path.

    Loading by path (not by dotted import) skips ``omnimarket``'s package
    ``__init__`` and therefore needs no installed dependencies — the canonical
    module is stdlib-only by design.

    Args:
        module_path: Path to ``merge_control/hold_marker.py``.

    Returns:
        The imported module, exposing ``HOLD_MARKER_RE``, ``evaluate_merge_hold``
        and ``EnumMergeHoldStatus``.

    Raises:
        CanonicalVocabularyUnavailableError: If the file is missing, cannot be
            loaded, or does not expose the expected surface.
    """
    if not module_path.is_file():
        raise CanonicalVocabularyUnavailableError(
            f"canonical hold vocabulary not found at {module_path} — the gate "
            "refuses rather than assuming no PR is held"
        )
    # The module name must be unique per path: these loads are also driven from
    # tests against mutated copies, and reusing one name would let a cached
    # entry answer for a different file.
    module_name = (
        "omnimarket_merge_control_hold_marker__ci_"
        + hashlib.sha256(str(module_path).encode("utf-8")).hexdigest()[:16]
    )
    spec = importlib.util.spec_from_file_location(module_name, module_path)
    if spec is None or spec.loader is None:
        raise CanonicalVocabularyUnavailableError(
            f"could not build an import spec for {module_path}"
        )
    module = importlib.util.module_from_spec(spec)
    # MUST be registered before exec_module: the canonical module defines a
    # ``@dataclass(frozen=True, slots=True)``, and building a slots dataclass
    # re-creates the class and looks itself up via
    # ``sys.modules[cls.__module__]``. Without this line that lookup returns
    # ``None`` and the import dies with a bare
    # ``'NoneType' object has no attribute '__dict__'``.
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    except Exception as exc:
        sys.modules.pop(module_name, None)
        raise CanonicalVocabularyUnavailableError(
            f"canonical hold vocabulary at {module_path} failed to import: {exc}"
        ) from exc
    for attribute in ("HOLD_MARKER_RE", "evaluate_merge_hold", "EnumMergeHoldStatus"):
        if not hasattr(module, attribute):
            raise CanonicalVocabularyUnavailableError(
                f"canonical hold vocabulary at {module_path} does not expose "
                f"{attribute!r} — refusing to guess a verdict"
            )
    return module


@dataclass(frozen=True, slots=True)
class PrFacts:
    """The observed PR surfaces plus which source they came from.

    Attributes:
        title: The PR title, or ``None`` if no source supplied one.
        labels: The PR's label names, or ``None`` if no source supplied them.
            An empty tuple means "observed, and the PR has no labels".
        source: ``"live"``, ``"payload"``, or ``"none"`` — recorded so the log
            names which surface the verdict rests on.
        live_error: Why the live fetch was skipped or failed, when it was.
    """

    title: str | None
    labels: tuple[str, ...] | None
    source: str
    live_error: str | None = None


def parse_labels_json(raw: str | None) -> tuple[str, ...] | None:
    """Parse the ``toJSON(github.event.pull_request.labels)`` payload.

    Args:
        raw: The JSON array of label objects, or ``None``/empty when the
            workflow did not supply it.

    Returns:
        The label names, ``()`` when the PR genuinely has no labels, or ``None``
        when the labels surface was not observed at all. The ``()`` vs ``None``
        distinction is load-bearing — the canonical evaluator treats ``None`` as
        unobserved, never as clear.
    """
    if raw is None:
        return None
    stripped = raw.strip()
    if not stripped:
        return None
    try:
        parsed = json.loads(stripped)
    except json.JSONDecodeError:
        return None
    if not isinstance(parsed, list):
        return None
    names: list[str] = []
    for entry in parsed:
        if isinstance(entry, Mapping):
            name = entry.get("name")
            if isinstance(name, str):
                names.append(name)
        elif isinstance(entry, str):
            names.append(entry)
    return tuple(names)


def fetch_live_pr(
    *,
    api_url: str,
    repo: str,
    pr_number: str,
    token: str,
    opener: Callable[[urllib.request.Request], Any] | None = None,
) -> dict[str, Any]:
    """Fetch current PR state from the GitHub API.

    Args:
        api_url: API root, resolved from the endpoint authority by
            :func:`resolve_github_rest_url`.
        repo: ``owner/name``.
        pr_number: The PR number as a string.
        token: A token with ``pull-requests: read``.
        opener: Injection seam for tests; defaults to ``urllib.request.urlopen``.

    Returns:
        The decoded PR object.

    Raises:
        urllib.error.URLError: On transport failure.
        ValueError: On a non-object or undecodable response.
    """
    request = urllib.request.Request(
        url=f"{api_url.rstrip('/')}/repos/{repo}/pulls/{pr_number}",
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "omnimarket-hold-gate",
        },
    )
    open_url = opener or (
        lambda req: urllib.request.urlopen(req, timeout=_API_TIMEOUT_SECONDS)
    )
    with open_url(request) as response:
        payload = json.loads(response.read().decode("utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("GitHub returned a non-object PR payload")
    return payload


def resolve_pr_facts(
    env: Mapping[str, str],
    *,
    opener: Callable[[urllib.request.Request], Any] | None = None,
    authority_path: Path = URL_AUTHORITY_FILE,
) -> PrFacts:
    """Resolve the PR title and labels, preferring live state over the payload.

    Live state wins when readable because it is current: setting or clearing a
    hold and re-running this job must take effect without a new push, which is
    what makes criterion 4 hold on the CI path. The payload is the fallback, so
    the gate still functions with no token and no network.

    Args:
        env: The process environment (injected for testability).
        opener: Injection seam for the HTTP call.
        authority_path: Path to the endpoint authority (injected for tests).

    Returns:
        The observed facts and their source.

    Raises:
        UrlAuthorityUnavailableError: When a live fetch is warranted but the
            endpoint authority cannot supply the base URL. Not degraded to the
            payload on purpose: a broken authority is a repo-integrity fault,
            and the alternative to raising is a hardcoded host.
    """
    payload_title = env.get("PR_TITLE") or None
    payload_labels = parse_labels_json(env.get("PR_LABELS_JSON"))

    token = env.get("GH_TOKEN") or env.get("GITHUB_TOKEN") or ""
    repo = env.get("GH_REPO") or env.get("GITHUB_REPOSITORY") or ""
    pr_number = env.get("PR_NUMBER") or ""

    live_error: str | None = None
    if token and repo and pr_number:
        try:
            live = fetch_live_pr(
                api_url=resolve_github_rest_url(authority_path),
                repo=repo,
                pr_number=pr_number,
                token=token,
                opener=opener,
            )
        except (
            urllib.error.URLError,
            OSError,
            ValueError,
            json.JSONDecodeError,
        ) as exc:
            live_error = (
                f"live PR fetch failed ({exc}); falling back to the event payload"
            )
        else:
            live_title = live.get("title")
            raw_labels = live.get("labels")
            live_labels: tuple[str, ...] | None = None
            if isinstance(raw_labels, list):
                live_labels = tuple(
                    str(entry["name"])
                    for entry in raw_labels
                    if isinstance(entry, Mapping) and isinstance(entry.get("name"), str)
                )
            if isinstance(live_title, str) or live_labels is not None:
                return PrFacts(
                    title=live_title if isinstance(live_title, str) else None,
                    labels=live_labels,
                    source="live",
                )
            live_error = "live PR fetch returned neither a title nor labels"
    else:
        live_error = "no token/repo/PR number in env; live refresh not attempted"

    if payload_title is not None or payload_labels is not None:
        return PrFacts(
            title=payload_title,
            labels=payload_labels,
            source="payload",
            live_error=live_error,
        )
    return PrFacts(title=None, labels=None, source="none", live_error=live_error)


def evaluate_pr(
    env: Mapping[str, str],
    *,
    module_path: Path = CANONICAL_HOLD_MODULE,
    opener: Callable[[urllib.request.Request], Any] | None = None,
    authority_path: Path = URL_AUTHORITY_FILE,
) -> tuple[int, str]:
    """Render the gate verdict for the current event.

    Args:
        env: The process environment.
        module_path: Path to the canonical vocabulary (injected for tests).
        opener: Injection seam for the HTTP call.
        authority_path: Path to the endpoint authority (injected for tests).

    Returns:
        ``(exit_code, human_readable_report)``.
    """
    event_name = env.get("GITHUB_EVENT_NAME", "")
    if event_name not in PR_EVENTS:
        return (
            EXIT_CLEAR,
            f"not applicable: event {event_name!r} carries no pull request. "
            "A merge hold is a PR-scoped predicate and code reaches a protected "
            "branch through a PR, which is gated here.",
        )

    try:
        canonical = load_canonical_hold_module(module_path)
    except CanonicalVocabularyUnavailableError as exc:
        return EXIT_HELD, f"FAIL (fail-closed): {exc}"

    try:
        facts = resolve_pr_facts(env, opener=opener, authority_path=authority_path)
    except UrlAuthorityUnavailableError as exc:
        return EXIT_HELD, f"FAIL (fail-closed): {exc}"
    decision = canonical.evaluate_merge_hold(title=facts.title, labels=facts.labels)
    status = canonical.EnumMergeHoldStatus

    provenance = f"hold surfaces read from: {facts.source}"
    if facts.live_error:
        provenance += f" ({facts.live_error})"

    if decision.status is status.HELD:
        return EXIT_HELD, (
            "FAIL — this PR is HELD against landing.\n"
            f"  matched token : {decision.matched_token!r}\n"
            f"  matched on    : {decision.matched_source}\n"
            f"  {provenance}\n"
            "\n"
            "The hold marker is honored by every merge consumer through this "
            "required check: while it matches, the PR cannot be required-green, "
            "so the Codex merge controller, `gh pr merge`, auto-merge and the "
            "ONEX merge node all refuse it alike.\n"
            "\n"
            "To release: remove the marker from the PR title and labels, then "
            "re-run this job (or push). The vocabulary is defined once, in "
            "src/omnimarket/merge_control/hold_marker.py."
        )

    if decision.status is status.INDETERMINATE:
        return EXIT_HELD, (
            "FAIL (fail-closed) — the hold state is UNREADABLE.\n"
            f"  {decision.reason}\n"
            f"  {provenance}\n"
            "\n"
            "Neither a PR title nor a label set was observed, so the marker "
            "could not be probed at all. An unreadable hold state is treated as "
            "held and never decays to clear (OMN-15483 criterion 2)."
        )

    report = f"PASS — no hold marker on this PR.\n  {decision.reason}\n  {provenance}"
    if decision.unobserved_sources:
        report += (
            "\n  NOTE: this clear is partial — "
            f"{', '.join(decision.unobserved_sources)} was not observed."
        )
    return EXIT_CLEAR, report


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entrypoint.

    Args:
        argv: Command-line arguments (defaults to ``sys.argv[1:]``).

    Returns:
        Process exit code: 0 clear, 1 held/indeterminate/unloadable.
    """
    parser = argparse.ArgumentParser(
        description=(
            "Fail when a PR carries the canonical merge-hold marker (OMN-15483). "
            "Registered in the CI Summary strict set so a held PR can never be "
            "required-green for ANY merge consumer."
        )
    )
    parser.add_argument(
        "--module-path",
        type=Path,
        default=CANONICAL_HOLD_MODULE,
        help="Path to the canonical hold_marker.py (default: the in-repo module).",
    )
    args = parser.parse_args(argv)

    code, report = evaluate_pr(os.environ, module_path=args.module_path)
    if code == EXIT_CLEAR:
        print(report)
        print("::notice::Merge Hold Gate: no hold marker on this PR.")
    else:
        print(report, file=sys.stderr)
        first_line = report.splitlines()[0]
        print(f"::error::Merge Hold Gate: {first_line}", file=sys.stderr)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
