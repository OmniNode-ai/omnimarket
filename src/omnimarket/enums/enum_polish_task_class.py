from enum import StrEnum


class EnumPolishTaskClass(StrEnum):
    """Polish task taxonomy. Phase 1 wires the first three; Phase 2 wires the rest."""

    # Phase 1 (mechanical — this plan)
    AUTO_MERGE_ARM = "auto_merge_arm"
    REBASE = "rebase"
    CI_RERUN = "ci_rerun"

    # Phase 2 (LLM-routed — reserved slots, no effect nodes yet)
    THREAD_REPLY = "thread_reply"
    CONFLICT_HUNK = "conflict_hunk"
    CI_FIX = "ci_fix"

    # Sentinel: classifier cannot determine a safe action — escalate to human
    STUCK = "stuck"
