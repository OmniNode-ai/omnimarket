# OmniMarket

OmniMarket provides portable ONEX workflow packages and contract-driven automation.
Documentation: [public knowledge base](https://github.com/OmniNode-ai/knowledge_base).

## ONEX CLI customer setup

This is the OMN-23413 preview runbook. Use matching reviewed wheels from the
preview build. These changes are not claimed to be available in a public package
release. A source-checkout test is not proof that the installed wheels work.

## Install the preview

Have Python 3.12 or 3.13 and `uv` installed already. Obtain the three reviewed
wheels and their checksums from the same preview build: `omnibase_core`,
`omnimarket`, and `omninode_claude`. The examples below use placeholder paths;
replace them with the supplied files. No repository checkout is needed.

```sh
CORE_WHEEL=/absolute/path/to/omnibase_core-VERSION-py3-none-any.whl
MARKET_WHEEL=/absolute/path/to/omnimarket-VERSION-py3-none-any.whl
CLAUDE_WHEEL=/absolute/path/to/omninode_claude-VERSION-py3-none-any.whl

uv tool install --no-python-downloads --python 3.12 \
  --with "$CORE_WHEEL" --with "$CLAUDE_WHEEL" \
  --with-executables-from omnibase-core "$MARKET_WHEEL"

onex --help
onex setup --help
onex config --help
onex plugins list
```

The core wheel supplies the `onex` executable and extensible command groups.
The market wheel supplies setup, model connections, delegation and dashboard
capabilities. The Claude wheel supplies the local marketplace and canonical
legacy hook installer with its dependency closure. An older core can show only
`config get` and `config init`; this preview needs the matching core extension
that also exposes `validate`, `export`, `import`, `profiles`, and `effective`.

This command does not install Python, Ollama, models, Docker, or Claude Code.
It explicitly installs the selected Python packages and their dependencies.
If `onex` is not on PATH, inspect `uv tool dir --bin` and configure that directory
in your shell using your normal shell setup. Check `command -v onex` before use.
Future public installation instructions must name actual published compatible
versions; do not replace these files with an assumed unreleased PyPI version.

## Guided setup

```sh
onex setup --workspace "$HOME/Code/onex" --interactive
```

The welcome banner reads “Welcome To OmniNode Delegation Onboarding.” Setup
loads available capabilities and recommended choices from installed manifests,
shows the plan, and asks before applying it. The workspace is a configuration
location; setup does not clone the old collection of repositories.

Ollama is optional. Install it, start it and prepare models separately if you
choose it. ONEX accepts its endpoint, lists the models that server exposes, and
uses the selected models. It neither starts Ollama nor downloads its models.
Other compatible servers use the same named endpoint model with their own key.
Hosted provider catalogs come from the provider registry and are read using
your key. Keys use hidden input or stdin, never command arguments or plan JSON.

Catalog listing is not an inference proof. Guided availability checks require
separate consent for one-token requests and any provider charges. Large catalogs
ask for a bounded shortlist before probing; all listed IDs remain visible.
Unavailable models are excluded, throttled models are marked busy, and models
not reached before the request budget are marked unprobed. Setup's selected
model proof must still succeed through the actual delegation route.

## An already-running endpoint

Replace `MODEL_SERVER_URL` and `MODEL_ID` with your server and a model it serves.
Do not put a key into the URL. A named connection keeps its endpoint, credentials
and model order separate from other servers.

```sh
onex models discover --url MODEL_SERVER_URL --json
onex models connect my-server --url MODEL_SERVER_URL --model MODEL_ID
onex models connections --json
onex models refresh-connection my-server --json
onex models test-connection my-server --yes --json

onex setup --workspace "$HOME/Code/onex" \
  --connection my-server --component delegation --component dashboard \
  --yes --no-interactive --json

onex delegate "Explain what a hash table does" --json
onex dashboard status --json
onex dashboard open
```

Add `--api-key` to `models connect` when the endpoint requires authentication.
Repeat `--model` in preference order to select several models. Use
`onex models select-connection my-server MODEL_ID` to make one the first route.
`onex models test-connection` deliberately sends model requests; review their
possible cost before passing `--yes`. A successful result identifies the selected
model and a canonical delegation receipt. Discovery alone makes no such claim.

For direct setup, the equivalent endpoint inputs are `--provider ollama`,
`--url MODEL_SERVER_URL`, `--model MODEL_ID` and optionally `--connection NAME`.
Authenticated Ollama uses `--endpoint-auth api-key`. `local` is keyless;
`openai_compatible` expects the server's API key. These are connection choices,
not runtime installers.

## Claude plugin, hooks and gateway

Install and sign in to Claude Code yourself. The customer plugin is loaded from
the installed local marketplace assets. The developer profile is an explicit
`--developer` choice that requires its existing developer workspace.

```sh
onex setup --workspace "$HOME/Code/onex" \
  --component claude-plugin --component claude-hooks --interactive
onex claude hooks status --json
```

The legacy delegation and accounting hooks use
`POST_SKILL_DELEGATION_ENFORCER` (bit 11). Their canonical installer installs the
complete dependency closure. Auxiliary gateway guards have their own owned
settings rows. Turning off legacy hooks does not stop the model gateway process.
Restart Claude sessions after changing hook registration or gateway settings.

Routing every Claude model request requires a gateway and an upstream that
actually implements Anthropic Messages, including the necessary tool and stream
behavior. OpenAI-compatible model listing alone does not establish that support.
Declare native support only for an endpoint you operate or have verified:

```sh
onex models connect claude-server --url MODEL_SERVER_URL --model MODEL_ID \
  --native-messages
onex setup --workspace "$HOME/Code/onex" --component claude-gateway \
  --gateway-connection claude-server --gateway-model MODEL_ID --interactive
onex claude status --json
onex claude start
```

The gateway binds to loopback and owns its authenticated process and Claude
binding. If the upstream fails the protocol proof, setup reports the failure;
it does not silently route those requests elsewhere. Preserve an existing
conflicting binding and resolve the reported conflict before retrying.

## Observe, repair and profiles

```sh
onex setup --workspace "$HOME/Code/onex" --action verify --json
onex setup --workspace "$HOME/Code/onex" --action repair --interactive
onex config effective --json
onex config export > onex-plan.json
onex config validate onex-plan.json --json
onex config import onex-plan.json --name my-profile --json
onex config profiles list --json
onex config export --profile my-profile > selected-plan.json
onex setup --plan selected-plan.json --interactive
```

Profiles contain desired choices, not keys. Import saves a profile and does not
apply it. A plan file cannot be combined with inline selections. For a read-only
profile run, set its `action` to `verify` before `setup --plan`. Normal verify
reads saved state without repeating model inference or optional completion
effects. Repair resumes incomplete components and rechecks successful ones.

If shell preflight reports conflicting aliases or environment settings, inspect
the named files. `--set-aside` explicitly backs up eligible conflicting lines
for the current invocation; it is not permission to delete unrelated shell state.
Unavailable OS tools and denied permissions produce a named fix.

## Capabilities and optional completion

```sh
onex plugins list --json
onex plugins info dashboard
onex plugins disable dashboard --json
onex plugins enable dashboard --json
onex plugins install /absolute/path/to/customer_capability-VERSION-py3-none-any.whl
onex plugins update /absolute/path/to/customer_capability-NEW_VERSION-py3-none-any.whl
onex plugins run CAPABILITY COMMAND --help
onex plugins uninstall CAPABILITY --yes
```

External capabilities must use the canonical package format and declare an
`onex.setup` manifest. Installing an arbitrary package without that manifest
does not make it a capability. Dependencies, cycles, conflicts and configuration
schemas are validated before activation. Package installation and upgrade use
the existing package owner; ONEX does not introduce another package manager.
Built-in capabilities are disabled rather than uninstalled individually.
Disposal preserves unrelated and later user-edited state, or refuses with an
action-required result when ownership cannot be established.

Optional `local-stack` accepts your existing Compose and topology files via
`--compose-file` and `--topology-file`. It does not install Docker. `cloud` setup
is not currently available. `completion-audio` and `completion-notification`
are never recommended automatically. Select either explicitly; the review
explains its OS effect. `--completion-name` supplies an optional user-entered
name. System voice and desktop notification commands run once, are bounded,
and are not retried automatically after an uncertain attempt. Notification
command success records submission, not proof that the OS displayed it.

## Upgrade and remove

To upgrade this preview, use the reviewed replacement three-wheel set with the
same `uv tool install` command and `--reinstall`, then run setup verify and repair
as needed. Package replacement and setup reconciliation are separate actions.
Keep your state directories; upgrades should not require deleting your profile.

Before removing the CLI, stop or disable its owned effects while it is installed:

```sh
onex plugins disable claude-gateway --json
onex plugins disable claude-hooks --json
onex plugins disable claude-plugin --json
onex dashboard stop --json
uv tool uninstall omnimarket
```

Review any ownership refusal instead of forcing removal of user-edited settings.
Tool removal does not erase `~/.onex`, `~/.onex_state`, your workspace or the
credential database. Remove a named server's owned credential explicitly, when
wanted, with `onex models remove-connection NAME --yes` before uninstalling.
Do not delete those directories merely to make a retry pass.

## Repeatable preview evidence

The aggregate test environment must install the exact core, market and Claude
wheels and provide `ONEX_TEST_CLI`, `ONEX_TEST_WHEEL`,
`ONEX_TEST_CORE_WHEEL`, `ONEX_TEST_CLAUDE_WHEEL` and
`ONEX_E2E_REQUIRE_COMPLETE=1`. The packaged journey and installed legacy hook
journey emit JSON artifacts, compare installed resources to wheel bytes, and
check no-op reruns and owned cleanup. Endpoint protocol fixtures and controlled
Claude-login/audio/notification executables are identified as fixtures. Actual
model and actual Claude-client proof are separate runs against an explicitly
provided endpoint; record their receipts and do not relabel fixture evidence.
