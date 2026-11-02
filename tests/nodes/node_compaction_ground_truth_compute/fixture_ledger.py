# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A synthetic ledger prefix covering every kind, closed and irrelevant rows included.

Session: orchestrator lane ``orch-a``; its child lanes are read from CLAIM ``parent=``.
Window opens 2026-11-01T10:00:00Z. Boundaries: T1 12:30, T2 14:00, T3 16:00 (all 2026-11-01).
"""

from __future__ import annotations

D = "2026-11-01"
WINDOW_START = f"{D}T10:00:00Z"
T1 = f"{D}T12:30:00Z"
T2 = f"{D}T14:00:00Z"
T3 = f"{D}T16:00:00Z"


def s(hhmmss: str) -> str:
    return f"{D}T{hhmmss}Z"


PRE_RULING = s("08:00:00")
CLAIM_OLD = s("09:00:00")
R1, R2, R3, R4, R5, CORR5 = (
    s("10:05:00"),
    s("10:10:00"),
    s("10:20:00"),
    s("10:30:00"),
    s("10:40:00"),
    s("10:45:00"),
)
C1, C2 = s("11:00:00"), s("11:05:00")
CLAIM_X, CLAIM_Y, CLAIM_OTHER, TERM_X = (
    s("11:10:00"),
    s("11:15:00"),
    s("11:20:00"),
    s("11:30:00"),
)
H1, H2, H3, H4 = s("12:00:00"), s("12:01:00"), s("12:02:00"), s("12:03:00")
M1, M2, M3, M4 = s("12:10:00"), s("12:11:00"), s("12:12:00"), s("12:13:00")
DRAFT1, DRAFT2 = s("12:14:00"), s("12:15:00")
M5, M6, M7, M9 = s("12:20:00"), s("12:21:00"), s("12:22:00"), s("12:23:00")
REL_H2, ACK_M2, ANS_M3, ACK_M6 = (
    s("12:40:00"),
    s("12:41:00"),
    s("12:42:00"),
    s("12:43:00"),
)
M8 = s("12:50:00")
R6, TERM_Y = s("15:00:00"), s("15:10:00")

P = "principal=op"
PREFIX: tuple[str, ...] = (
    f'{PRE_RULING} | RULING | lane=orch-a | {P} | ticket=OMN-1 | "Before the window." | pre',
    f"{CLAIM_OLD} | CLAIM | lane=lane-old | {P} | ticket=OMN-2 | parent=orch-a | old claim",
    f'{R1} | RULING | lane=orch-a | {P} | ticket=OMN-3 | "Use blue." | blue',
    f'{R2} | RULING | lane=orch-a | {P} | ticket=OMN-3 | amends={R1} | "Use blue, except in docs." | amend',
    f'{R3} | RULING | lane=orch-a | {P} | ticket=OMN-3 | "Use tabs." | tabs',
    f'{R4} | RULING | lane=orch-a | {P} | ticket=OMN-3 | supersedes={R3} | "Use spaces." | spaces',
    f'{R5} | RULING | lane=orch-a | {P} | ticket=OMN-3 | "Ship on friday." | ship',
    f"{CORR5} | CORRECTION | lane=orch-a | {P} | ticket=OMN-3 | corrects={R5} | operator_span=0:4 | said monday",
    f"{C1} | OPERATOR-CONSENT | lane=orch-a | {P} | ticket=OMN-4 | APPROVED SCOPE: edit docs | OUT OF SCOPE: deploy",
    f"{C2} | OPERATOR-CONSENT | lane=orch-a | {P} | ticket=OMN-4 | APPROVED SCOPE: one merge | OUT OF SCOPE: deploy",
    f"{CLAIM_X} | CLAIM | lane=lane-x | {P} | ticket=OMN-5 | parent=orch-a | closed claim",
    f"{CLAIM_Y} | CLAIM | lane=lane-y | {P} | ticket=OMN-6 | parent=orch-a | open claim",
    f"{CLAIM_OTHER} | CLAIM | lane=other-lane | {P} | ticket=OMN-7 | parent=orch-b | other family",
    f"{TERM_X} | TERMINAL | lane=lane-x | {P} | ticket=OMN-5 | friction=none | outcome=done | closes-CLAIM={CLAIM_X} | done",
    f"{H1} | HOLD | lane=orch-a | {P} | id={H1}-orch-a | to=all | surface=runtime | held with no release",
    f"{H2} | HOLD | lane=orch-a | {P} | id={H2}-orch-a | to=all | surface=deploy | released later",
    f"{H3} | HOLD | lane=orch-a | {P} | id={H3}-orch-a | to=all | surface=docs | until={D}T13:00:00Z | expires",
    f"{H4} | HOLD | lane=orch-b | {P} | id={H4}-orch-b | to=all | surface=other | other family hold",
    f"{M1} | MSG | from=orch-a | {P} | id={M1}-orch-a | to=operator | qkey=text:aa | which database should we use for the cache",
    f"{M2} | MSG | from=orch-a | {P} | id={M2}-orch-a | to=operator | qkey=text:bb | may the nightly job run on the staging host",
    f"{M3} | MSG | from=orch-a | {P} | id={M3}-orch-a | to=operator | qkey=text:cc | should the report go to the whole team",
    f"{M4} | MSG | from=orch-a | {P} | id={M4}-orch-a | to=operator | qkey=text:dd | SENT-ON-BEHALF (FACT) to the vendor: the invoice dispute is filed",
    f"{DRAFT1} | STATUS | lane=lane-y | {P} | ticket=OMN-6 | draft=/tmp/draft1.md | thread=T-100 | draft reply written",
    f"{DRAFT2} | STATUS | lane=lane-y | {P} | ticket=OMN-6 | draft=/tmp/draft2.md | thread=T-200 | draft note written",
    f"{M5} | MSG | from=lane-z | {P} | id={M5}-lane-z | to=orch-a | ticket=OMN-8 | report waiting for an acknowledgement",
    f"{M6} | MSG | from=lane-w | {P} | id={M6}-lane-w | to=orch-a | ticket=OMN-8 | report acknowledged later",
    f"{M7} | MSG | from=orch-b | {P} | id={M7}-orch-b | to=lane-q | ticket=OMN-9 | other family message",
    f"{M9} | MSG | from=lane-x | {P} | id={M9}-lane-x | to=lane-y | ticket=OMN-5 | child to child, not the session lane",
    f"{REL_H2} | RELEASE | lane=orch-a | {P} | re={H2}-orch-a | released",
    f"{ACK_M2} | ACK | from=operator | {P} | to=orch-a | re={M2}-orch-a | yes",
    f"{ANS_M3} | MSG | from=operator | {P} | to=orch-a | re={M3}-orch-a | answer to the report question",
    f"{ACK_M6} | ACK | from=orch-a | {P} | to=lane-w | re={M6}-lane-w | acknowledged",
    f"{M8} | MSG | from=orch-a | {P} | id={M8}-orch-a | to=operator | qkey=text:ee | SENT-ON-BEHALF (FACT) to the team: note sent from /tmp/draft2.md thread T-200",
    f'{R6} | RULING | lane=orch-a | {P} | ticket=OMN-3 | "Rename the module." | rename',
    f"{TERM_Y} | TERMINAL | lane=lane-y | {P} | ticket=OMN-6 | friction=none | outcome=done | closes-CLAIM={CLAIM_Y} | done",
)
