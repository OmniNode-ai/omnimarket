# OmniMarket

OmniMarket provides portable ONEX workflow packages and contract-driven automation.
Documentation: [public knowledge base](https://github.com/OmniNode-ai/knowledge_base).


## Model connections

Choose a hosted provider or connect an existing model server during `onex setup`.
Ollama is optional: install it and run your chosen model separately. ONEX only
registers the endpoint and tests the selected model; it does not install Ollama,
start its service, or download models.

```sh
onex setup --workspace "$HOME/Code/onex" --provider ollama \
  --url http://127.0.0.1:11434/v1 --model YOUR_RUNNING_MODEL
```

For an authenticated Ollama endpoint, add `--endpoint-auth api-key`. Enter the
key at the hidden prompt, or provide it on stdin for an unattended run. Keys do
not belong in command arguments or setup plan files. Authenticated endpoints use
the canonical OpenAI-compatible credential slot; an existing slot for another
server is preserved and reported as a conflict.

Use `--provider local --url URL --model MODEL` for another keyless compatible
server, or `--provider openai_compatible --url URL --model MODEL` for a server
that requires a key. A comma-separated `--model` list keeps the chosen order.
Use `onex setup --action verify --workspace "$HOME/Code/onex"` to inspect the
saved setup without provisioning or sending another model request.
