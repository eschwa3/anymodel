"""Portable web denylist (docs/adr/0001-worker-web-access.md, "Portable launch denylist").

This module is the reference implementation of the denylist **file format** --
any harness (not just this codebase) can port the format below and match this
module's test vectors (`tests/data/denylist_vectors.txt`) to confirm its own
matcher agrees with ours.

## File format

- UTF-8 text, one entry per line.
- `#` starts a comment. A comment may take a whole line or trail an entry on
  the same line. Blank lines (and comment-only lines) are ignored.
- An entry is a domain, e.g. `example.com`. There is no scheme, path, port,
  wildcard (`*`), or regex syntax -- any of those make the line invalid.
- Matching is case-insensitive, a trailing dot is ignored, and Unicode
  entries are normalized to IDNA A-label form (`xn--...`) so `münchen.de`
  and `xn--mnchen-3ya.de` are the same entry.
- **An entry matches itself and every subdomain.** `example.com` blocks
  `a.b.example.com`. It does not block `notexample.com` (matching is by
  dot-separated label, never by string suffix).
- A leading `!` marks an **exception**: it re-allows that domain and its
  subdomains under an otherwise-blocked parent, e.g. `!raw.githubusercontent.com`
  beneath a blocked `githubusercontent.com`.
- **The longest (most specific) matching entry wins.** If an exception and a
  deny entry name the exact same domain, the deny entry wins.
- A line is invalid (and `parse`/`load_denylist` raise `ValueError` naming the
  1-based line number) if, once whitespace and a trailing comment are
  stripped, what remains: contains a scheme (`http://...`) or a `/` (a path),
  contains whitespace, contains a port or is an IP literal (anything with a
  `:`, or that parses as an IPv4 address), contains a `*`, is a single label
  (no `.`, e.g. `localhost`), has an empty label (`a..b.com`, `.example.com`),
  or is empty after a leading `!` is stripped.

## Sources, merged by `load_denylist`

1. The bundled `web-denylist.txt` shipped with this package.
2. The user file at `${XDG_CONFIG_HOME:-~/.config}/anymodel-subagents/web-denylist.txt`
   (via `config.config_path()`). Missing is fine; present-but-invalid raises.
3. `extra` -- entries from `config.yaml`'s `web_denylist_extra`.
"""

from __future__ import annotations

import importlib.resources
import ipaddress
import re
from collections.abc import Iterable

# Strict LDH (letters/digits/hyphen) host, at least two labels -- applied after
# IDNA encoding. Mirrors tools/web.py's _HOST_RE: refuses a literal "%" (typed
# directly, or folded from a fullwidth "%" by IDNA's NFKC normalization) so a
# host like "x.webhook%2esite" can't sneak into the denylist, or slip past
# is_blocked() as an unrecognized-and-therefore-allowed host -- normalization
# failure is fail-closed (blocked), so this makes is_blocked() correctly block
# such lookalikes instead of comparing a nonsense string that matches nothing.
_HOST_RE = re.compile(r"[a-z0-9-]+(\.[a-z0-9-]+)+")


def _normalize_host(raw: str) -> str:
    """Normalize a bare domain (no leading `!`) to lowercase IDNA A-label form.

    Raises ValueError with a short, specific reason on anything invalid.
    """
    work = raw.strip()
    if not work:
        raise ValueError("empty domain")
    if any(ch.isspace() for ch in work):
        raise ValueError("domain must not contain whitespace")

    work = work.lower()
    if "://" in work or "/" in work:
        raise ValueError("domain must not include a scheme or path")
    if "*" in work:
        raise ValueError("wildcards are not allowed")

    work = work.removesuffix(".")
    if not work:
        raise ValueError("empty domain")

    # A colon covers both an explicit port and any IPv6 literal (bracketed or
    # not); an IPv4 literal has no colon, so it needs its own check below.
    if ":" in work:
        raise ValueError("domain must not include a port or IP literal")
    try:
        ipaddress.ip_address(work)
    except ValueError:
        pass
    else:
        raise ValueError("IP literals are not allowed")

    labels = work.split(".")
    if len(labels) < 2:
        raise ValueError("single-label domains are not allowed")
    if any(not label for label in labels):
        raise ValueError("domain has an empty label")

    try:
        ascii_domain = work.encode("idna").decode("ascii")
    except UnicodeError as exc:
        raise ValueError(f"invalid domain: {exc}") from exc
    ascii_domain = ascii_domain.lower()
    if not _HOST_RE.fullmatch(ascii_domain):
        raise ValueError("domain contains invalid characters")
    return ascii_domain


def parse(text: str) -> list[str]:
    """Parse denylist file text into normalized entries (`!`-prefixed = exception).

    Raises ValueError naming the 1-based line number of the first invalid line.
    """
    entries: list[str] = []
    for lineno, raw_line in enumerate(text.splitlines(), start=1):
        line = raw_line.split("#", 1)[0].strip()
        if not line:
            continue
        is_exception = line.startswith("!")
        body = line[1:].strip() if is_exception else line
        if is_exception and not body:
            raise ValueError(f"line {lineno}: empty domain after '!'")
        try:
            domain = _normalize_host(body)
        except ValueError as exc:
            raise ValueError(f"line {lineno}: {exc}") from exc
        entries.append(f"!{domain}" if is_exception else domain)
    return entries


class Denylist:
    """A parsed, normalized denylist. Build via `parse()` + this constructor,
    or use `load_denylist()` for the bundled + user + config merge.
    """

    def __init__(self, entries: Iterable[str]) -> None:
        deny: set[str] = set()
        allow: set[str] = set()
        for entry in entries:
            is_exception = entry.startswith("!")
            body = entry[1:] if is_exception else entry
            try:
                domain = _normalize_host(body)
            except ValueError as exc:
                raise ValueError(f"invalid denylist entry {entry!r}: {exc}") from exc
            (allow if is_exception else deny).add(domain)
        self.deny = frozenset(deny)
        self.exceptions = frozenset(allow)

    def is_blocked(self, host: str) -> bool:
        """True if `host` (any case, IDNA or Unicode, optional trailing dot) is denied.

        A host that fails normalization is blocked -- fail closed.
        """
        try:
            normalized = _normalize_host(host)
        except ValueError:
            return True
        labels = normalized.split(".")
        for i in range(len(labels)):
            candidate = ".".join(labels[i:])
            if candidate in self.deny:
                return True
            if candidate in self.exceptions:
                return False
        return False


def load_denylist(extra: Iterable[str] = ()) -> Denylist:
    """Bundled list + user file (XDG config dir) + `extra` (config.yaml's web_denylist_extra)."""
    # Imported here (not at module top) to avoid any import-order surprises
    # with config.py during this feature's parallel build.
    from anymodel_subagents.config import config_path

    entries: list[str] = []

    bundled_text = (
        importlib.resources.files("anymodel_subagents")
        .joinpath("web-denylist.txt")
        .read_text(encoding="utf-8")
    )
    try:
        entries.extend(parse(bundled_text))
    except ValueError as exc:
        raise ValueError(f"bundled web-denylist.txt: {exc}") from exc

    user_path = config_path().parent / "web-denylist.txt"
    if user_path.exists():
        try:
            user_text = user_path.read_text(encoding="utf-8")
        except OSError as exc:
            raise ValueError(f"{user_path}: could not read: {exc}") from exc
        try:
            entries.extend(parse(user_text))
        except ValueError as exc:
            raise ValueError(f"{user_path}: {exc}") from exc

    entries.extend(extra)
    return Denylist(entries)


def to_claude_code_rules(denylist: Denylist) -> tuple[list[str], list[str]]:
    """(`WebFetch(domain:...)` deny rules, warnings) for Claude Code `permissions.deny`.

    Claude Code's `domain:` rule matches an exact host only, so each deny entry
    becomes two rules: the apex and a `*.` form for subdomains. `!` exceptions
    have no Claude Code equivalent (a deny rule always wins there), so a deny
    entry with an exception at or below it is skipped and reported as a warning
    instead of silently emitting an over-broad rule.
    """
    rules: set[str] = set()
    warnings: list[str] = []
    for domain in denylist.deny:
        shadowing = next(
            (
                exception
                for exception in denylist.exceptions
                if exception == domain or exception.endswith(f".{domain}")
            ),
            None,
        )
        if shadowing is not None:
            warnings.append(
                f"skipped {domain!r}: exception {shadowing!r} has no Claude Code equivalent"
            )
            continue
        rules.add(f"WebFetch(domain:{domain})")
        rules.add(f"WebFetch(domain:*.{domain})")
    return sorted(rules), sorted(warnings)
