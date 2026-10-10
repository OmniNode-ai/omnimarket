# Synthetic work ledger (test fixture)

2026-10-01T00:00:01Z | CLAIM | lane=lane-a | actor=script:fixture | model=none | ticket=OMN-1001 | worktree=none | host=h1 | run=run-a | scope=parser work | est ~1 lane-hours; displaces nothing; (OMN-1001)
2026-10-01T00:00:01Z | STATUS | lane=lane-a | ticket=OMN-1001 | step one of the parser
2026-10-01T00:05:00Z | CLAIM | lane=lane-b | actor=script:fixture | model=none | ticket=OMN-1002 | related=OMN-1003 | worktree=none | lease=lease-7 | est ~2 lane-hours; displaces nothing; (OMN-1002)
2026-10-01T00:10:00Z | HOLD | lane=lane-b | id=2026-10-01T00:10:00Z-lane-b | to=all | repo=omnimarket | pr=omnimarket#12 | ticket=OMN-1002 | hold the merge until the parser lands
2026-10-01T00:11:00Z | HOLD | lane=lane-b | id=2026-10-01T00:11:00Z-lane-b | to=lane-c | surface=lab-runtime | until=2020-01-01T00:00:00Z | an expired surface lease
2026-10-01T00:12:00Z | HOLD | lane=lane-b | id=2026-10-01T00:12:00Z-lane-b | to=lane-c,lane-d | surface=lab-runtime | until=2099-01-01T00:00:00Z | a surface lease still in force
2026-10-01T00:13:00Z | HOLD | lane=lane-b | to=all | pr=omnimarket#13 | a legacy hold with no id
2026-10-01T00:14:00Z | HOLD | lane=lane-e | id=2026-10-01T00:14:00Z-lane-e | to=all | repo=omnibase_core | a repo-wide hold
2026-10-01T00:20:00Z | MSG | from=lane-a | id=2026-10-01T00:20:00Z-lane-a | to=lane-c | ticket=OMN-1001 | please review the parser change
2026-10-01T00:21:00Z | MSG | from=lane-a | id=2026-10-01T00:21:00Z-lane-a | to=all | ticket=OMN-1001 | the parser change is on the bus
2026-10-01T00:22:00Z | MSG | from=lane-b | id=2026-10-01T00:22:00Z-lane-b | to=lane-d | ticket=OMN-1002 | answered later
2026-10-01T00:23:00Z | ACK | from=lane-d | id=2026-10-01T00:23:00Z-lane-d | re=2026-10-01T00:22:00Z-lane-b | ticket=OMN-1002 | seen
2026-10-01T00:30:00Z | RELEASE | lane=lane-e | id=2026-10-01T00:30:00Z-lane-e | re=2026-10-01T00:14:00Z-lane-e | ticket=OMN-1004 | repo-wide hold lifted
2026-10-01T00:40:00Z | RULING | lane=lane-o | ticket=OMN-1001 | question=Ship the parser first? | kind=decision | "yes"
2026-10-01T00:41:00Z | OPERATOR-CONSENT | lane=lane-o | ticket=OMN-1002 | approved=one restart | "go"
2026-10-01T00:50:00Z | FRICTION | lane=lane-a | ticket=OMN-1001 | existing=OMN-1001 | cost=10m | the parser test was slow
  a continuation line of the friction row
2026-10-01T01:00:00Z | TERMINAL | lane=lane-a | ticket=OMN-1001 | friction=OMN-1001 | closes-CLAIM=2026-10-01T00:00:01Z | outcome=done | worktree=none | parser landed
2026-10-01T01:05:00Z | CLAIM | lane=lane-a | actor=script:fixture | model=none | ticket=OMN-1005 | worktree=none | est ~1 lane-hours; displaces nothing; (OMN-1005)
2026-10-01T01:10:00Z | CLAIM | lane=lane-c | actor=script:fixture | model=none | ticket=OMN-1006 | pr=omnimarket#12 | worktree=none | host=h2 | est ~1 lane-hours; displaces nothing; (OMN-1006)
2026-10-01T01:15:00Z | STATUS | lane=lane-c | ticket=OMN-1006 | rebased omnimarket#12 on the parser
2026-10-01T01:20:00Z | TERMINAL | lane=lane-c | ticket=OMN-1006 | friction=none | outcome=handed-off | worktree=none | handed off omnimarket#12
2026-10-01T01:25:00Z | STATUS | lane=work-ledger-parity | actor=script:work-ledger-parity | model=none | day=2026-09-30 | file_rows=10 | projection_rows=10 | missing=0 | extra=0 | state_mismatches=0 | unexplained=0 | backfilled=0 | exact=yes | Work-ledger parity receipt for UTC day 2026-09-30
2026-10-01T01:30:00Z | CORRECTION | lane=lane-b | ticket=OMN-1002 | corrects=2026-10-01T00:05:00Z | the lease is lease-8
2026-10-01T01:35:00Z | STATUS | lane=lane-b | ticket=OMN-1002 | lease=lease-8 | still on the parser follow-up
2026-10-01T01:40:00Z | HOLD | lane=lane-e | id=2026-10-01T01:40:00Z-lane-e | to=lane-d | pr=omnimarket#14 | until=2020-01-01T00:00:00Z | a hold with no surface stays in force past its until
2026-10-01T01:45:00Z | CLAIM | lane=lane-f | actor=script:fixture | model=none | ticket=OMN-1007 | worktree=none | est ~1 lane-hours; displaces nothing; (OMN-1007)
2026-10-01T01:45:00Z | TERMINAL | lane=lane-f | ticket=OMN-1007 | friction=none | outcome=done | worktree=none | closed in the same second, naming no claim
