# Delegation-Cost Projection Lane

This is the local, startable lane for the delegation-cost demo projections. It
does not deploy, restart, or mutate `.201`.

## Scope

The wrapper starts only these projection consumers:

- `projection-delegation`
- `projection-llm-cost`
- `projection-savings`

The existing `docker-compose.projection.yml` is intentionally broader: it
defines six projection services and defaults `KAFKA_BROKERS` to
`192.168.86.201:19092` when unset. For local demo work, use the wrapper below
instead of the compose file.

## Required Local Environment

Create or point at a local env file:

```bash
OMNIDASH_ANALYTICS_DB_URL=postgresql://...
KAFKA_BROKERS=localhost:19092
```

`KAFKA_BOOTSTRAP_SERVERS` is also accepted; the wrapper maps it to
`KAFKA_BROKERS` for the shared projection runner. The wrapper never prints env
values or secrets.

The wrapper fails before startup when:

- the env file is missing
- `OMNIDASH_ANALYTICS_DB_URL` is missing
- both `KAFKA_BROKERS` and `KAFKA_BOOTSTRAP_SERVERS` are missing
- any of those endpoints points at `192.168.86.201`

## Commands

Validate preflight only:

```bash
scripts/run_delegation_cost_projection_process.sh --check
```

Run under a foreground supervisor:

```bash
scripts/run_delegation_cost_projection_process.sh
```

Run in the background:

```bash
scripts/run_delegation_cost_projection_process.sh --detach
```

Check and stop background processes:

```bash
scripts/run_delegation_cost_projection_process.sh --status
scripts/run_delegation_cost_projection_process.sh --stop
```

Use a non-default env file:

```bash
OMNIMARKET_PROJECTION_ENV_FILE=/path/to/local.env \
  scripts/run_delegation_cost_projection_process.sh --detach
```

Logs and PID files are written under:

```text
.onex_state/delegation-cost-projection/
```

That directory is local process state and should not be committed.
