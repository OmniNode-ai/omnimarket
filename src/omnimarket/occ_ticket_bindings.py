# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The ONE Linear read that resolves a ticket's criterion bindings.

OMN-18332. Two producers mint OCC evidence companions and both run inside the
same effects runtime:

* ``node_occ_state_effect`` -> ``node_occ_companion_compute`` (the "compute"
  producer, ``runner: node_occ_companion_compute`` on its receipts), and
* ``node_pr_lifecycle_fix_effect`` -> ``OccCompanionEmitter`` (the "born" path,
  ``runner: node_pr_lifecycle_fix_effect``), which mints the majority.

The transcription first landed on the compute producer only, inline in
``HandlerOccStateEffect``. Measured on the deployed effects container between
its 2026-09-14T03:38:44Z restart and 06:1xZ: nine companions minted, seven by
the born path, and NOT ONE carrying a ``binds_ac`` key. The born path had no
Linear read at all, so a ticket whose author had declared five falsifiers --
``OMN-18333`` returns five, four of them accepted, from this very reader --
minted a contract that claimed none of them.

This module exists so that cannot recur. The read is a module-level function
that both producers import; wiring it to one producer and not the other is now
a thing you have to do on purpose rather than a thing that happens by default.

Fail-closed, in the one direction that matters
----------------------------------------------
Every failure -- no key in the environment, a scope the application lacks, a
transport error, a GraphQL error list, an unresolvable ticket, a ticket that
declared no falsifiers -- returns an EMPTY tuple. Empty renders exactly the
contract both producers render today, so a Linear outage costs the bindings and
never the mint. The reader can therefore never ACCEPT something wrongly; its
worst case is a criterion that holds at the closer, which is where an unread
declaration already holds.
"""

from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.request
from typing import Final

from omnimarket.config.service_endpoints import LINEAR_GRAPHQL_URL
from omnimarket.occ_ac_transcription import (
    ModelTranscribedBinding,
    transcribe_ac_bindings,
)
from omnimarket.occ_creation_revision import (
    HISTORY_QUERY,
    ISSUE_QUERY,
    LINEAR_API_KEY_ENV,
    build_ticket_declaration,
)

__all__ = [
    "LINEAR_TIMEOUT_S",
    "linear_graphql",
    "read_ticket_ac_bindings",
]

logger = logging.getLogger(__name__)

#: Ceiling on one Linear round trip. The bindings are a nice-to-have on a mint
#: that must complete either way, so this is deliberately short.
LINEAR_TIMEOUT_S: Final[float] = 30.0


def linear_graphql(query: str, variables: dict[str, object]) -> dict[str, object]:
    """One Linear GraphQL round trip, from the EFFECT that is allowed one.

    Returns an EMPTY mapping on every failure -- no key in the environment, a
    scope the application lacks, a transport error, a GraphQL error list. The
    caller turns that into "no declaration read", which turns into no bindings.
    The API key is read from the environment by name and is never logged, never
    returned, and never rendered into a contract; the error messages are
    surfaced, the credential is not.
    """
    api_key = os.environ.get(LINEAR_API_KEY_ENV, "")
    if not api_key:
        # Deliberately a fixed string with no arguments. The variable NAME is
        # not itself a secret, but a logging call whose argument is derived
        # from the credential read is exactly the shape a scanner cannot tell
        # apart from logging the credential -- and arguing with the scanner is
        # worse than having nothing to argue about.
        logger.warning(
            "no Linear API key in the environment; no criterion will be "
            "accepted automatically"
        )
        return {}
    payload = json.dumps({"query": query, "variables": variables}).encode("utf-8")
    request = urllib.request.Request(
        LINEAR_GRAPHQL_URL,
        data=payload,
        headers={"Content-Type": "application/json", "Authorization": api_key},
    )
    try:
        with urllib.request.urlopen(request, timeout=LINEAR_TIMEOUT_S) as response:
            body = json.load(response)
    except (urllib.error.URLError, TimeoutError, ValueError, OSError) as error:
        # The exception CLASS only. A urllib error can carry the request it
        # failed on, headers included, so rendering the object into a log is a
        # credential-disclosure path even when it usually is not.
        logger.warning(
            "Linear read failed (%s), accepting nothing", type(error).__name__
        )
        return {}
    errors = body.get("errors") if isinstance(body, dict) else None
    if errors:
        logger.warning(
            "Linear returned errors, accepting nothing: %s",
            [str(entry.get("message")) for entry in errors if isinstance(entry, dict)],
        )
        return {}
    data = body.get("data") if isinstance(body, dict) else None
    return data if isinstance(data, dict) else {}


def read_ticket_ac_bindings(ticket: str) -> tuple[ModelTranscribedBinding, ...]:
    """One cited ticket's criterion bindings, transcribed from its own text.

    The read half of OMN-18332 step 6, and the only one. It resolves the
    ticket's CREATION revision from ``documentContentHistory`` and hands both
    that and the live description to the pure transcriber, which decides
    accepted-versus-draft per criterion. Nothing here judges whether a
    criterion is satisfied; it copies a declaration the author already wrote.

    Returns an empty tuple on every failure and on a ticket that declared no
    falsifiers -- see this module's docstring for why those two are the same
    answer.
    """
    try:
        issue = linear_graphql(ISSUE_QUERY, {"id": ticket}).get("issue")
        document = issue.get("documentContent") if isinstance(issue, dict) else None
        history: object = None
        if isinstance(document, dict) and document.get("id"):
            container = linear_graphql(HISTORY_QUERY, {"id": str(document["id"])}).get(
                "documentContentHistory"
            )
            if isinstance(container, dict):
                history = container.get("history")
        declaration = build_ticket_declaration(ticket, issue, history)
    except Exception:
        logger.warning(
            "criterion transcription failed for %s; the companion is minted "
            "with no bindings",
            ticket,
            exc_info=True,
        )
        return ()
    if declaration is None:
        return ()
    return transcribe_ac_bindings(
        live_description=declaration.description,
        creation_revision=declaration.creation_revision,
        created_at=declaration.created_at,
        creator_id=declaration.creator_id,
    )
