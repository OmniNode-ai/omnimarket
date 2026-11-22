# Lane dispatch MSG format

The lane dispatcher places lanes on a lab host so the orchestrator's own machine places none. The orchestrator asks for
placements with one ledger `MSG` row addressed to `labdispatch`. The dispatcher parses it with
`omnimarket.nodes.node_lane_dispatch_intake_effect.handlers.handler_lane_dispatch_intake.parse_dispatch_rows`; this page is the
format that parser accepts.

## The row

```text
<ts> | MSG | from=<lane> | principal=<principal> | id=<ts>-<lane> | to=labdispatch | requests=<cell> | <free text>
```

- `to` names the dispatcher, alone or in a comma list.
- `id` is the row's timestamp, a hyphen and the sending lane; the dispatcher keys each request as `<id>#<lane>`, so a
  request is placed once however many ticks read the row.
- `requests` is the base64url encoding (no padding) of a JSON array of one to twenty placement requests.
- The free text is for readers; the dispatcher ignores it.

## A placement request

| key | value |
| --- | --- |
| `lane` | the new lane's name: lowercase letters, digits and hyphens, 3 to 63 characters, unique in the message |
| `ticket` | the ticket the lane works, `OMN-<n>` |
| `host` | a pool host such as `h201` pins the lane to it; `any` or an empty string lets placement choose |
| `task` | the task text the lane's brief is built from, at most 20000 characters |

A message with a bad `requests` cell, an invalid request or a repeated lane name is rejected whole: the dispatcher writes
one `STATUS` row with `status=rejected` and the reason, and places none of its requests.

## What the dispatcher writes

For each request, in order and one at a time, one row:

```text
<ts> | STATUS | lane=labdispatch | ticket=<ticket> | placed=<lane> | status=<placed|no-host|failed> | host=<host> | exit=<code> | msg=<id> | <detail>
```

`no-host` means no pool host admitted the lane; the request stays pending and is tried again on the next tick, with no
second row. `failed` means the brief did not pass its check or the runner refused; it is not retried.

## Worked example

Two requests in one message. The first pins its lane to `h201`; the second lets placement choose.

```text
2026-11-21T23:10:00Z | MSG | from=orchestrator-example | principal=example-principal | id=2026-11-21T23:10:00Z-orchestrator-example | to=labdispatch | requests=W3siaG9zdCI6ImgyMDEiLCJsYW5lIjoiZXhhbXBsZS1hLTAxMDAiLCJ0YXNrIjoiUmVhZC1vbmx5OiBwcmludCB0aGUgaG9zdG5hbWUgYW5kIGZpbmlzaC4iLCJ0aWNrZXQiOiJPTU4tMjY0MTIifSx7Imhvc3QiOiJhbnkiLCJsYW5lIjoiZXhhbXBsZS1iLTAxMDAiLCJ0YXNrIjoiUmVhZC1vbmx5OiBsaXN0IHRoZSBsYWJvcmF0b3J5IGhvc3QgbG9hZCBhbmQgZmluaXNoLiIsInRpY2tldCI6Ik9NTi0yNjQxMiJ9XQ | place two example lanes
```

The `requests` cell decodes to:

```json
[{"host":"h201","lane":"example-a-0100","task":"Read-only: print the hostname and finish.","ticket":"OMN-26412"},{"host":"any","lane":"example-b-0100","task":"Read-only: list the laboratory host load and finish.","ticket":"OMN-26412"}]
```
