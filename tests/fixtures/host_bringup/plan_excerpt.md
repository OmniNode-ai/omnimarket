# An excerpt shaped like the plan's graph section, for the generator tests

## 4. The design

| # | Not | A | Target | Table |
|---|---|---|---|---|
| 1 | `ignored` | outside | section | five |

## 5. The graph, in order

### Stage A: declare (launching host; no machine needed)

| # | Target | Needs | Already-satisfied check | Action | Falsifier | Ticket | Repo | h053 |
|---|---|---|---|---|---|---|---|---|
| 1 | `card` | none | the host's row validates against the strict table model and names a role | stop with the one field missing | a row with no role is refused and names the field | OMN-21876 | omnimarket; data in overlay | no |
| 2 | `ssh-config` | card | `ssh -G <host>` resolves target and user from the row | write one managed include on the launching host | remove the alias; only this target and what needs it run | mint | omnimarket | done |

### Stage B: the machine

| # | Target | Needs | Already-satisfied check | Action | Falsifier | Ticket | Repo | h053 |
|---|---|---|---|---|---|---|---|---|
| 3 | `reservations` | card | the row carries a dated person confirmation | person: make the reservation, then confirm it once | a row with no confirmation reads unsatisfied | none: stays human | none | done |
| 4 | `host-key` | reservations, ssh-config | the row holds the fingerprint | person once: compare the fingerprint | an unconfirmed key stops every target that uses ssh | mint | omnimarket | part |

## 6. One declaration, the rest derived

| # | Target | Needs | Already-satisfied check | Action | Falsifier | Ticket | Repo | h053 |
|---|---|---|---|---|---|---|---|---|
| 5 | `after-the-section` | card | not read | none | not read | mint | omnimarket | no |
