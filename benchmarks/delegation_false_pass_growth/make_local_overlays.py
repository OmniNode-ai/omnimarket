"""Write the three local-only bifrost overlays the in-memory generation used, inside the lab container.

The two chat endpoints come from the environment (BACKEND_A_CHAT_URL is the vLLM Qwen3.8-27B host, BACKEND_B_CHAT_URL the llama.cpp
Qwen3.8-27B host); the lab's own addresses are in the dev lane overlay, not in this file.
    dev.local.bifrost.yaml      both local rungs on backend A (the dev lane's own binding)
    dev.local202.bifrost.yaml   local-coder on A, local-heavy-reasoning on B (prose classes moved off the busy host)
    dev.local202b.bifrost.yaml  both rungs on B
Only the two rungs already in the packaged contract are declared, so the overlay stays valid against the image's omnimarket.
"""

import os

import yaml

a = os.environ["BACKEND_A_CHAT_URL"]  # url-authority-ok: lab one-shot
b = os.environ["BACKEND_B_CHAT_URL"]  # url-authority-ok: lab one-shot


def overlay(coder: str, heavy: str) -> dict:
    return {
        "backends": [
            {
                "backend_id": "local-coder",
                "endpoint_url": coder,
                "model_name": "Qwen3.8-27B",
            },
            {
                "backend_id": "local-heavy-reasoning",
                "endpoint_url": heavy,
                "model_name": "Qwen3.8-27B",
            },
        ]
    }


for name, (coder, heavy) in {
    "dev.local": (a, a),
    "dev.local202": (a, b),
    "dev.local202b": (b, b),
}.items():
    with open(f"/tmp/omn20032/{name}.bifrost.yaml", "w") as fh:
        yaml.safe_dump(overlay(coder, heavy), fh, sort_keys=False)
