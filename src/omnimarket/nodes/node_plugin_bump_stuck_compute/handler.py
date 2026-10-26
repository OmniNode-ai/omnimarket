"""Judge the post-merge plugin version bump pull request from the PR watcher's records (OMN-20497).

A merged plugin change reaches no host until the bump lands, because the plugin cache is keyed by version. On
2026-10-18 a bump pull request was force-pushed onto every new main tip, so its required check restarted on each
merge, and it sat open 90 minutes with six merged fixes running nowhere and nothing saying so. queue-status prints
one ``bump`` line per open bump and turns a bump open past the bound into a DEGRADED reason that names it. An
unread watcher state is said, never read as no bump.
"""

from __future__ import annotations

from datetime import UTC, datetime

from .models import ModelPluginBumpStuckRequest, ModelPluginBumpStuckResult


def _when(value: object) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


class HandlerPluginBumpStuck:
    def handle(
        self, request: ModelPluginBumpStuckRequest
    ) -> ModelPluginBumpStuckResult:
        if not isinstance(request.watcher, dict):
            return ModelPluginBumpStuckResult(
                lines=["bump    unread (no fresh PR watcher state)"]
            )
        bound = int(request.stuck_after.total_seconds() // 60)
        lines: list[str] = []
        reasons: list[str] = []
        actions: list[str] = []
        for key, rec in sorted((request.watcher.get("prs") or {}).items()):
            facts = rec.get("facts") if isinstance(rec, dict) else None
            if (
                not isinstance(facts, dict)
                or facts.get("head_ref") != request.branch
                or str(facts.get("state", "")).upper() != "OPEN"
            ):
                continue
            opened = _when(facts.get("created_at", ""))
            age = int((request.now - opened).total_seconds() // 60) if opened else None
            verdict = str((rec.get("ci") or {}).get("verdict") or "?")
            lines.append(
                f"bump    {key} open {'?' if age is None else f'{age}m'} ci={verdict} "
                f"head={str(facts.get('head_sha', ''))[:8] or '?'} pushed={facts.get('updated_at') or '?'}"
            )
            if age is not None and age > bound:
                reasons.append(
                    f"plugin version bump {key} open {age}m (over {bound}m), ci={verdict}: the plugin "
                    "changes merged since the last bump run on no host until it lands"
                )
                actions.append(
                    f"read why {key} has not landed: pr_state_local.py --pr {key} (its checks and head "
                    "push times), the plugin-version-bump runs (onex scm run-list --workflow "
                    "plugin-version-bump.yml) and the landing controller's ticks for it; never merge or "
                    "arm it by hand (OMN-20497)"
                )
        return ModelPluginBumpStuckResult(
            lines=lines or ["bump    none open"], reasons=reasons, actions=actions
        )
