"""Secret redaction for transcripts and logs."""

from __future__ import annotations

import base64
import json
import re
from typing import Any

# Common API-key-shaped patterns. Order matters only in that they're all applied.
# Every prefix-based pattern uses a negative lookbehind so it doesn't fire in the
# middle of an ordinary word (e.g. "risk-assessment-2024-01-01" must not match
# the generic `sk-...` pattern just because it contains "sk-" as a substring).
_NOT_WORD = r"(?<![A-Za-z0-9_])"
_KEY_PATTERNS = [
    re.compile(_NOT_WORD + r"sk-or-v1-[A-Za-z0-9]{16,}"),
    re.compile(_NOT_WORD + r"sk-ant-[A-Za-z0-9_-]{16,}"),
    re.compile(_NOT_WORD + r"sk-[A-Za-z0-9_-]{20,}"),
    re.compile(_NOT_WORD + r"ghp_[A-Za-z0-9]{16,}"),
    re.compile(_NOT_WORD + r"github_pat_[A-Za-z0-9_]{20,}"),
    re.compile(_NOT_WORD + r"xox[abprs]-[A-Za-z0-9-]{10,}"),
    re.compile(_NOT_WORD + r"AIza[A-Za-z0-9_-]{20,}"),
    re.compile(_NOT_WORD + r"AKIA[A-Z0-9]{12,}"),
]

# PEM-style private key blocks (RSA/EC/DSA/OPENSSH/ENCRYPTED/plain "PRIVATE KEY") are paired
# by position in `_redact_pem_blocks`, not matched with one BEGIN...END regex: redaction runs
# synchronously on the event loop over worker- and provider-controlled text, and a lazy
# `.*?` between the markers is quadratic (unbounded) or ~5 s/MB (bounded) on BEGIN-only spam.
_PEM_BEGIN = re.compile(r"-----BEGIN [A-Z0-9 ]{0,40}PRIVATE KEY-----")
_PEM_END = re.compile(r"-----END [A-Z0-9 ]{0,40}PRIVATE KEY-----")
_PEM_MAX_BODY = 16_384  # an RSA-16384 key is ~12 KB; anything longer isn't a key block


def _redact_pem_blocks(text: str) -> str:
    """Replace each BEGIN...first-following-END private key block, in one linear pass."""
    if "-----BEGIN " not in text or "-----END " not in text:
        return text
    ends = list(_PEM_END.finditer(text))
    if not ends:
        return text
    out: list[str] = []
    pos = 0  # everything before `pos` is already emitted (or redacted)
    e = 0
    for begin in _PEM_BEGIN.finditer(text):
        if begin.start() < pos:
            continue  # inside a block that was just redacted
        while e < len(ends) and ends[e].start() < begin.end():
            e += 1
        if e == len(ends):
            break
        if ends[e].start() - begin.end() > _PEM_MAX_BODY:
            continue
        out.append(text[pos : begin.start()])
        out.append("[REDACTED]")
        pos = ends[e].end()
    out.append(text[pos:])
    return "".join(out)


def _secret_variants(secret: str) -> set[str]:
    """Verbatim, base64, and JSON-escaped forms of a live secret value.

    A worker/model can smuggle a secret through a transcript in an encoded
    form (e.g. quoting it back inside a JSON blob, or a tool re-emitting it
    base64-encoded) without the raw bytes ever appearing literally.
    """
    variants = {secret}
    try:
        variants.add(base64.b64encode(secret.encode("utf-8")).decode("ascii"))
    except Exception:  # noqa: BLE001, S110 - redaction must never raise
        pass
    try:
        # json.dumps(secret) is `"..."`; strip the surrounding quotes to get
        # just the escaped form as it would appear embedded in JSON text.
        variants.add(json.dumps(secret)[1:-1])
    except Exception:  # noqa: BLE001, S110
        pass
    return variants


def redact(text: str, secrets: list[str] | None = None) -> str:
    """Replace known secret values and common key patterns in `text` with "[REDACTED]".

    `secrets` is a list of live secret values (e.g. the actual API key) to scrub verbatim
    (plus their base64/JSON-escaped forms), in addition to the generic key-shaped patterns
    this always checks for.
    """
    if not text:
        return text
    out = text
    for secret in secrets or []:
        if not secret:
            continue
        for variant in _secret_variants(secret):
            if variant:
                out = out.replace(variant, "[REDACTED]")
    for pattern in _KEY_PATTERNS:
        out = pattern.sub("[REDACTED]", out)
    return _redact_pem_blocks(out)


def redact_deep(obj: Any, secrets: list[str] | None = None) -> Any:
    """Recursively apply `redact()` to every string found in a nested structure."""
    if isinstance(obj, str):
        return redact(obj, secrets)
    if isinstance(obj, dict):
        return {k: redact_deep(v, secrets) for k, v in obj.items()}
    if isinstance(obj, list):
        return [redact_deep(v, secrets) for v in obj]
    if isinstance(obj, tuple):
        return tuple(redact_deep(v, secrets) for v in obj)
    return obj
