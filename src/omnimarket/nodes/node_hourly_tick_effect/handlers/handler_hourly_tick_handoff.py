"""One handoff per held human ask, written for the operator to answer.

A tick whose sweep held a human ask (no usable draft) used to end degraded and tell nobody. Record
now writes one entry per held comment to a drafts file (ticket, the question, why it was held, a
one-line suggested reply) and returns, per comment, the push text and the ledger detail the caller
sends: one push per comment, nothing posted to the tracker or chat, nothing sent to the person who
asked.

State is a small JSON map keyed by comment id, so a wakeup that records the same fire again, or a
later tick that holds the same comment, writes nothing new and never pushes twice. An entry stays
pending until ``mark_pushed`` is told its push went out, so a failed push is retried, never lost.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

from omnimarket.nodes.node_hourly_tick_effect.models.model_hourly_tick import (
    ModelHandoffPush,
    ModelHandoffReport,
    ModelHeldHandoff,
)

FIRE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")


def state_dir() -> Path:
    configured = os.environ.get("ONEX_STATE_DIR")
    return Path(configured) if configured else Path.home() / ".local/state/onex"


def ledger_path() -> Path:
    return state_dir() / "hourly-tick/handoffs.json"


def drafts_path(fire_id: str) -> Path:
    if not FIRE_ID.fullmatch(fire_id):
        raise ValueError("hourly-tick: a handoff needs a valid fire id")
    return state_dir() / "awaiting-reply" / f"{fire_id}-held-drafts.md"


def _plain(text: str) -> str:
    """One line, no pipe (a ledger cell), no question mark (a push is a statement), no end stop."""
    return (
        re.sub(r"\s+", " ", text.replace("|", "/").replace("?", ""))
        .strip()
        .rstrip(".!")
    )


def _load() -> dict[str, dict[str, str | None]]:
    path = ledger_path()
    if not path.is_file():
        return {}
    data = json.loads(path.read_text())
    if not isinstance(data, dict):
        raise ValueError(f"hourly-tick: {path} is not a JSON object")
    return data


def _save(state: dict[str, dict[str, str | None]]) -> None:
    path = ledger_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(state, indent=1, sort_keys=True) + "\n")
    temporary.replace(path)


def _entry_text(item: ModelHeldHandoff) -> str:
    return "\n".join(
        [
            f"## {item.ticket} - {item.author_role or 'commenter'} - {item.comment_url}",
            "",
            "Class: held human ask, no usable draft (hourly tick)",
            f"Question: {item.question}",
            f"Why held: {item.why_held}",
            f"Suggested reply: {item.suggested_reply}",
            "Nothing is posted. The reply needs the operator's yes.",
            "",
        ]
    )


def _push_for(comment_id: str, row: dict[str, str | None]) -> ModelHandoffPush:
    who = _plain(row.get("author_role") or "commenter")
    ticket = _plain(row.get("ticket") or "")
    url = _plain(row.get("comment_url") or "")
    file = row.get("drafts_file") or ""
    push = (
        f"Held reply on {ticket} for {who}: {_plain(row.get('question') or '')}. "
        f"Held because {_plain(row.get('why_held') or '')}. "
        f"Suggested reply: {_plain(row.get('suggested_reply') or '')}. "
        f"Handoff: {file}. Link: {url}. Nothing is posted; it needs the operator's yes."
    )
    ledger = (
        f"{who} {url}: held human ask on {ticket} from the hourly tick, "
        f"no usable draft ({_plain(row.get('why_held') or '').split(':', 1)[0]}); "
        f"draft at {file}, push sent, nothing sent to the person"
    )
    return ModelHandoffPush(comment_id=comment_id, push_text=push, ledger_detail=ledger)


def record_handoffs(
    items: list[ModelHeldHandoff], fire_id: str
) -> ModelHandoffReport | None:
    """Write the handoffs not yet written and report every one still waiting for its push."""
    if not items:
        return None
    state = _load()
    fresh = [item for item in items if item.comment_id not in state]
    path = drafts_path(fire_id)
    if fresh:
        header = f"# Held human asks from the hourly tick {fire_id}\n\n"
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".md.tmp")
        temporary.write_text(header + "\n".join(_entry_text(item) for item in fresh))
        temporary.replace(path)
        for item in fresh:
            state[item.comment_id] = {
                **{k: v for k, v in item.model_dump().items() if k != "comment_id"},
                "fire_id": fire_id,
                "drafts_file": str(path),
                "pushed_at": None,
            }
        _save(state)
    held_ids = {item.comment_id for item in items}
    pending = [
        _push_for(comment_id, row)
        for comment_id, row in sorted(state.items())
        if row.get("pushed_at") is None
        and (comment_id in held_ids or row.get("fire_id") != fire_id)
    ]
    return ModelHandoffReport(
        drafts_file=str(
            path if fresh else (state[next(iter(held_ids))].get("drafts_file") or path)
        ),
        written=len(fresh),
        already_handed_off=len(items) - len(fresh),
        pending_push=pending,
    )


def mark_pushed(comment_ids: list[str], now: str) -> list[str]:
    """Record that the push went out; an unknown id is refused, so a typo hides no pending one."""
    state = _load()
    unknown = [c for c in comment_ids if c not in state]
    if unknown:
        raise ValueError(f"hourly-tick: no handoff for comment {', '.join(unknown)}")
    for comment_id in comment_ids:
        state[comment_id]["pushed_at"] = now
    _save(state)
    return comment_ids
