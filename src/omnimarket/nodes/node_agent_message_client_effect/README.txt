Lab: host=h201 lane=codexmsg2-0000 command=`claude -p` (send, read) and `codex exec` (read, send) against the MCP server `onex agent-message serve-mcp --bus-lane dev`, plus `onex agent-message send|read` for the refusal control; observed=Claude sent event 4bc44ba76a942e93ed90d0b1765b9818ae6baee507d662e230364a744cedaf2d, Codex read it and sent event 8506031f5ffb6e31d0fa202100962106d727cc9e027ebecd7f654df1febef3bd, Claude read that one back, and the login with no grants was refused broker_refused (exit 3).

# Agent message client

`onex agent-message` lets an agent on any platform send a message to another agent and read the
messages addressed to it, over the event bus. The same two handlers answer the command line and a
stdio MCP server, so a send or a read behaves the same either way:

| Tool or command | Arguments |
| -- | -- |
| `send_agent_message` / `onex agent-message send` | `to`, `thread`, `kind`, `platform`, `text` |
| `read_agent_messages` / `onex agent-message read` | `to`, optional `thread`, `since`, `limit` |

`to` is a lane name or `platform:name`. `kind` is one of `message`, `question`, `answer`, `notice`.
`platform` is one of `claude`, `codex`, `glm`, `hermes`, `gemini`, `other`. The sender is
`$ONEX_LANE`, then `$ONEX_AGENT_IDENTITY`, then `<platform>:<platform>`, and the client stamps
`sent_at` in UTC. A send returns the event id; a read returns the same id for the same message.

## What a client needs

- `onex` on the `PATH`, installed from this package.
- A broker login that may write, describe and read the agent message topic. The broker address is
  never typed here: the agent message node's overlay names the lane (`dev`), and the lane
  declaration holds the address.
- The workspace config root, so the lane declaration and the node overlay can be read
  (`OMNIBASE_PATH`, and `ONEX_WORKSPACE_CONFIG_ROOT` where the workspace layout is not the default).
- For a SASL lane with no stored login, `KAFKA_SASL_USERNAME`, `KAFKA_SASL_PASSWORD`,
  `KAFKA_SECURITY_PROTOCOL` and `KAFKA_SASL_MECHANISM` in the environment of the process that runs
  the client. A host's stored lane login is preferred over these, so to act as a different login
  start the client with an empty `HOME`.

MCP clients start the server with a trimmed environment (Codex passes almost nothing through), so
what the server needs belongs in its registration, not in the shell that starts the client. The
simplest registration points the client at a small wrapper that sources the credential file, sets
the variables above and then runs `exec onex agent-message serve-mcp --bus-lane dev`. Keep the
credential file out of the registration and out of the repository; the registration names the
wrapper only.

Register the server in a scratch or project config while you try it, never in a config you rely on.

## Claude Code

```bash
claude mcp add agent-message -e ONEX_LANE=<lane> -- onex agent-message serve-mcp --bus-lane dev
```

`--scope project` writes the registration to `.mcp.json` in the current folder instead of the user
config. For a headless run that reads only that file and may call only the two tools:

```bash
claude -p --mcp-config .mcp.json --strict-mcp-config \
  --allowedTools "mcp__agent-message__send_agent_message mcp__agent-message__read_agent_messages" \
  "Call send_agent_message with to=<recipient>, thread=<thread>, kind=question, platform=claude, text=<text>."
```

## Codex

A table in `config.toml` (the user file `~/.codex/config.toml`, or the `config.toml` of a scratch
`CODEX_HOME` that links to your login's `auth.json`):

```toml
[mcp_servers.agent-message]
command = "onex"
args = ["agent-message", "serve-mcp", "--bus-lane", "dev"]
env = { ONEX_LANE = "<lane>" }
```

`codex exec` runs with approvals off and refuses an MCP tool call that needs approval
(`MCP tool call requires approval, but approval policy is never`). Approve the two tools in the
same file:

```toml
[mcp_servers.agent-message.tools.read_agent_messages]
approval_mode = "approve"

[mcp_servers.agent-message.tools.send_agent_message]
approval_mode = "approve"
```

## Gemini CLI

An entry under `mcpServers` in `~/.gemini/settings.json` with the same command and arguments:

```json
{
  "mcpServers": {
    "agent-message": {
      "command": "onex",
      "args": ["agent-message", "serve-mcp", "--bus-lane", "dev"],
      "env": { "ONEX_LANE": "<lane>" }
    }
  }
}
```

The lab run below did not exercise Gemini. Hermes takes the same command and arguments in its MCP
server list.

## Command line

A platform that cannot start an MCP server calls the command from a shell:

```bash
onex agent-message send --to codex:review --thread t-1 --kind question \
  --platform claude --text "please review the diff" --bus-lane dev
onex agent-message send --to some-lane --thread t-1 --kind answer \
  --platform codex --text -@answer.txt --bus-lane dev
onex agent-message read --to codex:review --thread t-1 --since 2026-11-23T13:00:00Z --limit 20 \
  --bus-lane dev --json
```

`--text` takes the text, `-@FILE` to read a file, or `-` for stdin. `--bus-lane dev` can be set once
as `$ONEX_AGENT_MESSAGE_BUS_LANE`. `--bus inmemory` is the offline bus for trying the command
without a broker.

A refusal prints one line on stderr and exits non-zero with no stack trace:

| Refusal | Exit |
| -- | -- |
| `missing_recipient`, `missing_thread`, `empty_text`, `unknown_platform`, `unknown_kind`, `bad_since`, `bad_limit` | 2 |
| `undeclared_broker`, `broker_refused`, `node_unavailable` | 3 |

A login that holds no grant on the topic is refused by the broker: the command prints
`broker_refused` with `ClusterAuthorizationFailedError` and exits 3.

## What the lab run showed

On the dev lane, one host, two logins that hold only the topic's write, describe and read rows,
and one thread:

1. A Claude Code client, scratch project config, sender `claude-test-a`, called
   `send_agent_message` to `codex-test-b` (kind `question`). Event id
   `4bc44ba76a942e93ed90d0b1765b9818ae6baee507d662e230364a744cedaf2d`.
2. A Codex client, scratch `CODEX_HOME`, sender `codex-test-b`, called `read_agent_messages` for
   `codex-test-b` on that thread and got that one message back with the same id, then called
   `send_agent_message` to `claude-test-a` (kind `answer`). Event id
   `8506031f5ffb6e31d0fa202100962106d727cc9e027ebecd7f654df1febef3bd`.
3. The Claude Code client called `read_agent_messages` for `claude-test-a` on the thread and got
   the Codex answer back with the same id.
4. The login with no grants ran `onex agent-message send` and `read`: both exited 3 with
   `broker_refused` (`ClusterAuthorizationFailedError`).

The message text was made up for the run.
