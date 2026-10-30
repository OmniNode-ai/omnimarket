# Fixture rolling work ledger: two members, each with a quiet session and a quiet dangling lane. Every value is fake.

2026-10-29T08:00:00Z | CLAIM | lane=sess-a | principal=fixture-member-a-dev | actor=claude:fixture | ticket=OMN-9000001 | worktree=none | scope=member a session
2026-10-29T08:00:00Z | CLAIM | lane=sess-b | principal=fixture-member-b-dev | actor=claude:fixture | ticket=OMN-9000002 | worktree=none | scope=member b session
2026-10-29T08:05:00Z | CLAIM | lane=lane-a | principal=fixture-member-a-dev | actor=claude:fixture | ticket=OMN-9000003 | parent=sess-a | repo=omnimarket | pr=omnimarket#901 | worktree=/fixture/wt/a | consent=2026-10-29T07:00:00Z | scope=member a lane
2026-10-29T08:05:00Z | CLAIM | lane=lane-b | principal=fixture-member-b-dev | actor=claude:fixture | ticket=OMN-9000004 | parent=sess-b | repo=omnimarket | pr=omnimarket#902 | worktree=/fixture/wt/b | scope=member b lane
2026-10-29T08:06:00Z | CLAIM | lane=child-a | actor=claude:fixture | ticket=OMN-9000005 | parent=sess-a | worktree=none | scope=unattributed child of member a's session
2026-10-29T08:06:00Z | CLAIM | lane=child-b | actor=claude:fixture | ticket=OMN-9000006 | parent=sess-b | worktree=none | scope=unattributed child of member b's session
2026-10-29T08:07:00Z | CLAIM | lane=orphan | actor=claude:fixture | ticket=OMN-9000007 | worktree=none | scope=unattributed, no parent
2026-10-29T08:08:00Z | CLAIM | lane=posing-child | principal=fixture-member-b-dev | actor=claude:fixture | ticket=OMN-9000008 | parent=sess-a | worktree=none | scope=member b's lane naming member a's session as parent
2026-10-29T08:09:00Z | CLAIM | lane=landing-sub-b | principal=fixture-member-b-dev | actor=claude:fixture | ticket=OMN-9000009 | parent=landing | worktree=none | scope=member b's landing sub-lane
2026-10-29T08:10:00Z | CLAIM | lane=done-a | principal=fixture-member-a-dev | actor=claude:fixture | ticket=OMN-9000010 | worktree=none | scope=closed below
2026-10-29T08:11:00Z | TERMINAL | lane=done-a | principal=fixture-member-a-dev | closes-CLAIM=2026-10-29T08:10:00Z-done-a | ticket=OMN-9000010 | outcome=landed | delegation=na:fixture | closed
