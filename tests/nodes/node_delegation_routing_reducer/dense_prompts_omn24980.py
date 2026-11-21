# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Deterministic prompts whose metered token count was read on the lab endpoint.

Each generator is pure: the same text on every host, so the reading recorded
beside its sha256 in ``fixtures/metered_token_readings.json`` stays attached to
the text the test sends through the router.
"""

from __future__ import annotations

import base64
import hashlib
import itertools
import json
import random
import string
import uuid

SAMPLE_CHARACTERS = 140000  # 35000 estimated tokens, the old input budget


def code_digit_named(characters: int) -> str:
    """Function definitions numbered ``f_0``, ``f_1`` ... (digits meter singly)."""
    line = "def f_{n}(a):\n    return a\n"
    parts: list[str] = []
    size = n = 0
    while size < characters:
        parts.append(line.format(n=n))
        size += len(parts[-1])
        n += 1
    return "".join(parts)[:characters]


def code_letter_named(characters: int) -> str:
    """Function definitions named by three letters (the proof prompt's shape)."""
    parts: list[str] = []
    size = 0
    for name in itertools.product(string.ascii_lowercase, repeat=3):
        parts.append("def f_{}(a):\n    return a\n".format("".join(name)))
        size += len(parts[-1])
        if size >= characters:
            break
    return "".join(parts)[:characters]


def digits(characters: int) -> str:
    rng = random.Random(7)
    return "".join(rng.choice("0123456789") for _ in range(characters))


def hex_lines(characters: int) -> str:
    lines = (hashlib.sha256(str(i).encode()).hexdigest() for i in itertools.count())
    return "\n".join(itertools.islice(lines, characters // 64 + 1))[:characters]


def uuid_lines(characters: int) -> str:
    ids = (str(uuid.uuid5(uuid.NAMESPACE_DNS, str(i))) for i in itertools.count())
    return "\n".join(itertools.islice(ids, characters // 37 + 1))[:characters]


def base64_blob(characters: int) -> str:
    raw = b"".join(hashlib.sha256(str(i).encode()).digest() for i in range(characters))
    return base64.b64encode(raw).decode()[:characters]


def json_floats(characters: int) -> str:
    rng = random.Random(7)
    return json.dumps([rng.random() for _ in range(characters // 10 + 1)])[:characters]


GENERATORS = {
    "code_digit_named": code_digit_named,
    "code_letter_named": code_letter_named,
    "digits": digits,
    "hex_lines": hex_lines,
    "uuid_lines": uuid_lines,
    "base64_blob": base64_blob,
    "json_floats": json_floats,
}


def sample(name: str, characters: int = SAMPLE_CHARACTERS) -> str:
    return GENERATORS[name](characters)


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()
