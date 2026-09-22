"""The untrusted-report wrapper, shared by everything that hands a worker's text to an orchestrator
(`results`/`wait` inline, and the `report.md` a job leaves in its job dir)."""

from __future__ import annotations

import re
import secrets

REPORT_TAG_RE = re.compile(
    "(?i)<(\\s{0,4}/?\\s{0,4}worker[\\s_.\u00ad\u200b\u200c\u200d\u2060\ufeff-]{0,4}report)"
)


def wrap_report(job_id: str, text: str) -> str:
    """Wrap a worker's final message as labelled, untrusted data.

    The reader is an LLM, not an XML parser, so a fixed closing tag isn't
    enough: a hostile report can spell tag-like text of its own
    (`</WORKER_REPORT>`, `< /worker_report>`, or a forged opening tag) and
    make what follows read as if it came from outside the untrusted report.
    Each wrapper therefore carries a fresh random boundary that is generated
    only after the worker finished -- the report cannot forge a matching
    closing tag because the boundary didn't exist when it ran -- and every
    tag-like prefix inside the text is neutralized (`<` -> `[`) so nothing
    inside can pass for one of the wrapper's own tags.
    """
    # Ids are server-generated (`j-` + hex), but this sits inside the opening tag: an id
    # that could spell `"`, `<`, `>` or a newline would forge a wrapper of its own.
    safe_job_id = "".join(ch for ch in (job_id or "") if ch.isalnum() or ch in "-_")[:64]
    body = text or ""
    # The boundary is hex, so the neutralization below can neither create nor
    # destroy a match; only the raw text itself can contain it verbatim.
    boundary = secrets.token_hex(8)
    while boundary in body:
        boundary = secrets.token_hex(8)
    body = REPORT_TAG_RE.sub(r"[\1", body)
    opening = f'<worker_report job="{safe_job_id}" trust="untrusted" boundary="{boundary}">'
    closing = f'</worker_report boundary="{boundary}">'
    return f"{opening}{body}{closing}"
