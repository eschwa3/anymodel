"""OS-level sandboxing for the Bash tool: macOS Seatbelt, Linux bwrap.

Threat model: the command-prefix allowlist in `bash.py` is only a second
layer. The worker LLM is untrusted and can, e.g., write an arbitrary test
file and then run `pytest` -- an allowlisted prefix -- to execute it. Real
containment comes from the OS sandbox built here: no network, writes
confined to the workspace + a scratch tmp dir, reads denied by default
except for the workspace/tmp and a curated set of read-only system and
toolchain locations needed to actually run test/lint/build tools.

`detect()` empirically probes for a working sandboxing mechanism rather than
just checking presence, since a binary can exist but be unusable (e.g.
missing entitlements, disabled by MDM policy, unprivileged container).

Seatbelt profile design (`build_seatbelt_argv`): deny-by-default rather than
`(allow default)` with denies layered on top. An adversarial review of the
previous `(allow default)` + denies profile found it left mach services
(keychain, pasteboard, Apple Events, LaunchServices) and almost the entire
filesystem reachable to a sandboxed worker. The rules below were arrived at
empirically: `sandbox-exec -f <profile> <real command>` run directly against
python3/pytest/git/node/sh, reading the exact failure (dyld's own
"file system sandbox blocked open()" message on stderr when the profile is
permissive enough for dyld to get that far, or a synchronous crash log under
`~/Library/Logs/DiagnosticReports/<binary>-*.ips` when it isn't -- `(deny
default)` alone made even `/bin/echo` abort silently in dyld's cache-finder
before any output, because dyld needs `file-read-data` on the *literal* path
"/" itself (opened directly, not reachable via any `subpath` rule) and
`file-map-executable` -- not just `file-read-data` -- on every directory it
dlopen()s a library from). The set of directories below is the result of
that process, not a list copied from documentation.
"""

from __future__ import annotations

import functools
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Literal

from anymodel_subagents.types import PolicyError

SandboxKind = Literal["seatbelt", "bwrap"]

_SEATBELT_EXE = "/usr/bin/sandbox-exec"
_PROBE_PROFILE = "(version 1)(allow default)"
_PROBE_TIMEOUT_S = 5.0

# ---------------------------------------------------------------------------
# Seatbelt read-allowlist: read-only system/toolchain locations.
#
# These are hardcoded, operator-controlled constants (never derived from a
# worker's input or environment), so they're safe to splice directly into
# the profile text -- unlike WORKSPACE/TMP/deny_read/extra_read, which are
# always passed as `-D` parameters (see `build_seatbelt_argv`'s docstring
# note on why: a path containing quotes/parens must never be able to inject
# into or corrupt the compiled profile).
# ---------------------------------------------------------------------------

_READ_ALLOW_SYSTEM_DIRS: tuple[str, ...] = (
    "/usr",
    "/bin",
    "/sbin",
    "/opt/homebrew",
    "/opt/local",
    "/usr/local",
    "/Library/Developer",
    # /Library/Preferences is deliberately NOT here: the git/xcrun shims were measured to
    # work without it, and it exposes ~60 host-fingerprint plists (network services, VPN,
    # MDM posture, printers) that a worker could report to the outside model.
    "/Library/Apple",  # e.g. /System/Library/PrivateFrameworks/* resolve here
    "/Applications/Xcode.app",
    "/System",
    "/Library/Frameworks",
    "/Library/Java",
    "/private/etc",  # makes /etc/passwd readable; not secret on macOS
    "/private/var/db/timezone",
    "/private/var/db/dyld",
    "/private/var/select",
)

# Per-user toolchain homes, relative to `Path.home()`. Read-only: never
# added to the write-allow list.
_READ_ALLOW_HOME_RELATIVE_DIRS: tuple[str, ...] = (
    ".pyenv",
    ".local/share/uv",
    ".local/bin",
    ".cache/uv",
    ".cargo/bin",
    ".cargo/registry",
    ".rustup",
    "go/pkg",
    ".nvm",
    ".volta",
    ".asdf",
    ".local/share/mise",
    ".npm",
    "Library/pnpm",
    "Library/Caches/pip",
    "Library/Caches/pypoetry",
    ".gradle/caches",
    ".m2/repository",
    # git reads these for user.name/user.email/core.* and the global
    # gitignore; needed empirically for `git status`/`git diff`/`git log`
    # to work at all under a restrictive profile (git aborts with "unable
    # to access '~/.gitconfig'" otherwise). Not secret material.
    ".config/git",
)
_READ_ALLOW_HOME_FILES: tuple[str, ...] = (".gitconfig",)

_DEV_READ_LITERALS: tuple[str, ...] = (
    "/dev/null",
    "/dev/zero",
    "/dev/random",
    "/dev/urandom",
    "/dev/tty",
)


def _probe_seatbelt() -> bool:
    if not Path(_SEATBELT_EXE).exists():
        return False
    try:
        proc = subprocess.run(
            [_SEATBELT_EXE, "-p", _PROBE_PROFILE, "/usr/bin/true"],
            capture_output=True,
            timeout=_PROBE_TIMEOUT_S,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return proc.returncode == 0


# Secret-shaped file names (final path component), as regex fragments. The file tools deny
# these through workspace.py; a sandboxed `cat`/`grep`/script would otherwise read them
# straight out of the re-allowed workspace subtree. Kept as a short list of families: the
# allowlist can never be the control here (a worker can always run its own test script).
#
# Every fragment uses `[^/]*` rather than `.*` for "anything": these fragments are
# spliced into a full-path regex wrapped as `(.*/)?{pat}$` (see `_secret_name_denies`),
# and Seatbelt's regex engine matches `.` against `/` too -- an internal `.*` lets the
# fragment itself span across path separators, over-denying an entire subtree from a
# single filename-shaped fragment (e.g. the old credential pattern's trailing
# `.*\.(ext)` group matched `credential_provider/README.txt` in full). `[^/]*` confines
# every fragment to the single final path component the wrapper already anchors it to.
#
# Split into two groups for `_secret_dir_name_denied` (the directory-vs-file rule): the
# CREDENTIAL/TOKEN family is subject to the "enterable package name" carve-out (`token/`,
# `credential_provider/` stay enterable as directories, see below); every GENERIC pattern
# denies with no such carve-out, same as it does for a file.
_GENERIC_SECRET_PATTERNS: tuple[str, ...] = (
    r"\.env",
    r"\.env\.[^/]*",
    r"[^/]*\.env",
    r"[^/]*\.env\.[^/]*",
    r"[^/]*\.(pem|key|p12|pfx|jks|ovpn|keystore)",
    r"id_(rsa|dsa|ecdsa|ed25519)[^/]*",
    r"[^/]*_rsa",
    r"\.git-credentials",
    r"\.(netrc|npmrc|pypirc)",
    # secreview-0110-fix2 finding 1: in-workspace `.aws/`, `.ssh/`, `.gnupg/` had no
    # Seatbelt rule at all, so a sandboxed `cat <ws>/.ssh/config` succeeded even though
    # the file tools already deny them via workspace._DENIED_COMPONENT_PATTERNS's exact
    # literal entries. These are bare literal names (no wildcard), matching that table:
    # the wrapper's `(.*/)?{pat}$` anchor denies the directory ENTRY itself (both as a
    # final-component "file" match and, via `_secret_dir_denies`'s inclusion of every
    # GENERIC pattern, the whole subtree beneath it -- and `_secret_dir_name_denied`
    # (bwrap's directory walk) consults this same GENERIC table, so `.aws`/`.ssh`/
    # `.gnupg` are masked there too). `.docker`/`.kube` are deliberately NOT added:
    # workspace.py's `_DENIED_COMPONENT_PATTERNS` doesn't deny them either, and denying
    # them here (but not in the file tools) could break a legitimate in-workspace
    # docker-compose/kubeconfig test fixture with no corresponding security gain.
    r"\.ssh",
    r"\.aws",
    r"\.gnupg",
    # secrets.*/.secrets.* (any suffix): the data-extension leaf check in the
    # credential/token family below misses compound/encrypted/backup spellings
    # (secrets.sops.yaml, secrets.enc, secrets.yaml.bak, secrets.age, secrets.db,
    # secrets.gpg, secrets.kdbx, secrets.tfvars, ...). Denied outright; a single
    # genuine source/prose extension is re-allowed below (mirrors
    # workspace._SECRET_SOURCE_EXTS: exact `secrets.<ext>`/`.secrets.<ext>` only, not
    # a compound name like `secrets.test.ts`).
    r"\.?secrets\.[^/]*",
    r"authorized_keys",
    # secret_key* (secret_key_base, secret_key.rb, ...): any name starting with
    # `secret_key`, mirroring workspace._DENIED_COMPONENT_PATTERNS's `secret_key*` glob.
    r"secret_key[^/]*",
)
_CREDENTIAL_TOKEN_FAMILY_PATTERNS: tuple[str, ...] = (
    # Credential files: any name containing "credential" with no extension at all, or
    # with a machine-config extension (same data-ext list as workspace._SECRET_DATA_EXTS).
    # Two fragments, not one `.*credentials?(...)?`: that single-regex form only matched
    # `credential[s]` as a bare name or immediately followed by an extension, missing
    # extensionless compound names (`credential-store`, `credential_helpers`) that
    # workspace.py's `"credential" in norm` substring check already denies as files. The
    # leading `\.?` matters: `os.path.splitext` (what workspace._secret_name_denied uses)
    # treats a single LEADING dot as a dotfile marker, not the start of an extension, so
    # `.credentials` has ext `""` there too and must still match this "no extension"
    # fragment, not just the two-or-more-component-name shapes.
    r"\.?[^/.]*credentials?[^/.]*",
    r"[^/]*credentials?[^/]*\.(json|yaml|yml|ini|toml|csv|txt|cfg|conf|env|xml|properties)",
    # Token/secret files: bare/leaf names plus data-extension spellings.
    r"token",
    r"secrets",
    r"\.secrets",
    r"[^/]*\.(token|secret|secrets)",
    r"([^/]*(\.|_|-))?token\.(json|yaml|yml|ini|toml|csv|txt|cfg|conf|env|xml|properties)",
    r"([^/]*(\.|_|-))?secrets\.(json|yaml|yml|ini|toml|csv|txt|cfg|conf|env|xml|properties)",
)
_SECRET_NAME_PATTERNS: tuple[str, ...] = (
    _GENERIC_SECRET_PATTERNS + _CREDENTIAL_TOKEN_FAMILY_PATTERNS
)
# Re-allowed after the denies (last match wins): templates that are meant to be read.
_ENV_EXAMPLE_EXCEPTION_PATTERN = r"\.env\.(example|sample|template|dist)"
_SECRETS_SOURCE_EXCEPTION_PATTERN = r"\.?secrets\.(py|pyi|ts|tsx|js|jsx|go|rs|rb|java|kt|md|rst)"
_SECRET_NAME_EXCEPTIONS: tuple[str, ...] = (
    _ENV_EXAMPLE_EXCEPTION_PATTERN,
    # secrets.py / secrets.md etc.: an exact `secrets.<ext>`/`.secrets.<ext>` source/prose
    # file, not secret data. No inner wildcard group: a compound name (`secrets.test.ts`,
    # `secrets.yaml.md`) is NOT exempted here -- it stays denied by the pattern above
    # (mirrors workspace._secret_name_denied's Family C). A wildcard group here
    # (`(?:.*\.)?`) would also repeat finding 1's bug: Seatbelt's regex engine is POSIX
    # ERE, which has no `(?:...)` non-capturing-group syntax.
    _SECRETS_SOURCE_EXCEPTION_PATTERN,
)
# The same tables compiled for Python's `re`: `_secret_files_under` (the bwrap side)
# matches workspace file names at argv-build time, case-insensitively. DOTALL so `.*`
# also spans a newline inside a (legal, if unwise) file name -- broader, never narrower.
_SECRET_NAME_RES: tuple[re.Pattern[str], ...] = tuple(
    re.compile(p, re.IGNORECASE | re.DOTALL) for p in _SECRET_NAME_PATTERNS
)
_SECRET_NAME_EXCEPTION_RES: tuple[re.Pattern[str], ...] = tuple(
    re.compile(p, re.IGNORECASE | re.DOTALL) for p in _SECRET_NAME_EXCEPTIONS
)
_GENERIC_SECRET_RES: tuple[re.Pattern[str], ...] = tuple(
    re.compile(p, re.IGNORECASE | re.DOTALL) for p in _GENERIC_SECRET_PATTERNS
)
_CREDENTIAL_TOKEN_FAMILY_RES: tuple[re.Pattern[str], ...] = tuple(
    re.compile(p, re.IGNORECASE | re.DOTALL) for p in _CREDENTIAL_TOKEN_FAMILY_PATTERNS
)
_ENV_EXAMPLE_EXCEPTION_RE = re.compile(_ENV_EXAMPLE_EXCEPTION_PATTERN, re.IGNORECASE | re.DOTALL)

# Secret-shaped DIRECTORY names: a directory component (not just a file's basename) that
# matches hides everything beneath it. `_SECRET_NAME_PATTERNS` above only anchors to the
# path's FINAL component (Seatbelt: the `(.*/)?{pat}$` suffix match; bwrap:
# `_secret_files_under` matches a bare filename) -- so a data file living *inside* such a
# directory (`secrets/README.md`, `aws_credentials/prod.json`) would slip through that
# check alone.
#
# Mirrors workspace.py's `_dir_component_denied`: denies when (a) the name is an exact
# (case-insensitive) match against `_SECRET_DIR_NAMES`; (b) the name starts with
# `secrets.`/`.secrets.` (no extension carve-out here -- a directory is never a genuine
# source file, so `secrets.py/` is still denied even though a *file* named `secrets.py`
# isn't); or (c) a CREDENTIAL_TOKEN_FAMILY match that isn't one of the enterable package
# shapes (bare `token`, or a `credential`-substring name whose last `[._-]`-separated
# segment isn't itself `credential`/`credentials`/`secret`/`secrets`) -- so `token/`,
# `credential_provider/`, `credential-store/`, `credential_helpers/` stay enterable, while
# `aws_credentials/`, `credentials.json/`, `app-secrets.json/`, `deploy_token.json/`,
# `prod.secret/` still hide everything beneath them. A GENERIC family match (`.env`, key
# files, `secret_key*`, ...) denies with no carve-out, same as the file rule.
#
# See `_secret_dir_name_denied` (the shared decision, used directly by the bwrap side),
# `_secret_dir_denies` (Seatbelt: a static regex approximation of the same decision,
# since Seatbelt evaluates against paths that may not exist at argv-build time), and
# `_secret_dirs_under` (bwrap: a pre-run walk that can just call the predicate directly).
_SECRET_DIR_NAMES: tuple[str, ...] = ("secrets", ".secrets", "credentials", ".credentials")
_SECRET_DIR_NAMES_CF: frozenset[str] = frozenset(n.casefold() for n in _SECRET_DIR_NAMES)

# When a CREDENTIAL_TOKEN_FAMILY match fires solely because of the `credential`
# substring, a directory of that name stays enterable unless its last `[._-]`-separated
# segment (extension stripped) is itself one of these words -- see
# `_secret_dir_name_denied`. Mirrors workspace._CREDENTIAL_DIR_ENTERABLE_EXCLUDED.
_CREDENTIAL_DIR_ENTERABLE_EXCLUDED = frozenset({"credential", "credentials", "secret", "secrets"})

# Seatbelt-only: a static regex approximation of the CREDENTIAL_TOKEN_FAMILY carve-out,
# for splicing into `_secret_dir_denies`'s profile text. Unlike
# `_CREDENTIAL_TOKEN_FAMILY_PATTERNS` (which matches `credential`/`token`/`secrets`
# ANYWHERE in a file name), this anchors the match to the name's FINAL
# `[._-]`-separated segment -- the same "last segment" rule `_secret_dir_name_denied`
# applies in Python -- so `credential_provider`/`credential-store` (credential substring
# in the middle) don't match, while `aws_credentials`/`credentials.json`
# (credential/credentials as the trailing segment) do. Bare `token`/`secrets`/`.secrets`
# are deliberately excluded: `token` must stay enterable, and `secrets`/`.secrets` are
# already covered by the exact-match `_SECRET_DIR_NAMES` rule.
_SECRET_DIR_FAMILY_PATTERNS: tuple[str, ...] = (
    # Not `([^/]*[._-])?`: empirically, Seatbelt's regex compiler mis-parses a bracket
    # expression ending in an unescaped `-` when it sits inside a `?`-quantified group
    # (`(deny ... (regex ...))` fails to compile at all with "unterminated bracket
    # expression", even though the identical bracket outside a group, or the same
    # separator set spelled as an alternation, both parse fine). `(\.|_|-)` avoids the
    # bracket-in-group shape entirely; every other fragment already uses this spelling.
    r"([^/]*(\.|_|-))?credentials?(\.[^/]*)?",
    r"[^/]*\.(token|secret|secrets)",
    r"([^/]*(\.|_|-))?token\.(json|yaml|yml|ini|toml|csv|txt|cfg|conf|env|xml|properties)",
    r"([^/]*(\.|_|-))?secrets\.(json|yaml|yml|ini|toml|csv|txt|cfg|conf|env|xml|properties)",
)


def _secret_dir_name_denied(name: str) -> bool:
    """True if a directory component named `name` should hide everything beneath it.

    Mirrors `workspace._dir_component_denied`; see the module comment above this
    function for the three rules. Independent implementation, built from this module's
    own tables -- pinned equal to workspace.py by tests/test_secfix_secret_names.py.
    """
    cf = name.casefold()
    if cf in _SECRET_DIR_NAMES_CF:
        return True
    if cf.startswith(("secrets.", ".secrets.")):
        return True
    if any(
        p.fullmatch(name) for p in _GENERIC_SECRET_RES
    ) and not _ENV_EXAMPLE_EXCEPTION_RE.fullmatch(name):
        return True
    if not any(p.fullmatch(name) for p in _CREDENTIAL_TOKEN_FAMILY_RES):
        return False
    if cf == "token":
        return False
    if "credential" not in cf:
        return True
    root, _ext = os.path.splitext(cf)
    last_segment = re.split(r"[._-]", root)[-1] if root else cf
    return last_segment in _CREDENTIAL_DIR_ENTERABLE_EXCLUDED


def _any_case(fragment: str) -> str:
    """Make the letters of a regex fragment match either case (Seatbelt has no /i flag)."""
    out: list[str] = []
    escaped = False
    for ch in fragment:
        if escaped or not ch.isalpha():
            out.append(ch)
            escaped = ch == "\\" and not escaped
        else:
            out.append(f"[{ch.lower()}{ch.upper()}]")
    return "".join(out)


# Seatbelt-only rendering of `_CREDENTIAL_TOKEN_FAMILY_PATTERNS`'s first fragment
# (`[^/.]*credentials?[^/.]*`, the extensionless-credential-substring FILE match).
#
# Empirically, Seatbelt's regex compiler cannot exclude BOTH `/` and `.` from one
# negated bracket: `[^/.]*` silently stops excluding `.` (as if written `[^/]*`), so the
# canonical pattern above would deny things like `credential_store.ts` (a source file
# that must stay readable) and, worse, span across a `/` the way finding 4 already fixed
# for other fragments. A single-char negation (`[^/]` or `[^.]` alone) works fine, and a
# POSITIVE class (list the allowed characters, never negate) sidesteps the bug entirely
# -- confirmed by isolating the failure down to a minimal repro (a negated bracket
# containing both `/` and `.`, in or out of a group, on this machine's sandbox-exec).
#
# This can't go through the generic per-letter `_any_case()` pass either: `_any_case`
# has no notion of a character-class range and would mangle `A-Za-z0-9` into
# `[aA]-[zZ][aA]-[zZ]0-9`. So it's pre-folded here (the `credential` literal only; the
# `A-Za-z0-9_-` range already covers both cases with no folding needed) and
# `_secret_name_denies`/`_secret_dir_denies` look it up in `_SEATBELT_PRECASED_OVERRIDES`
# to skip `_any_case` for it specifically.
_CREDENTIAL_CI = _any_case("credential")
#  `-` is placed FIRST in the bracket, not last: empirically, this dialect also
# mis-parses a bracket expression ending in an unescaped (or even backslash-escaped)
# trailing `-` as "unterminated", regardless of negation -- `[-A-Za-z0-9_]` parses fine,
# `[A-Za-z0-9_-]` and `[A-Za-z0-9_\-]` do not.
_FA1_SEATBELT_PATTERN = f"\\.?[-A-Za-z0-9_]*{_CREDENTIAL_CI}s?[-A-Za-z0-9_]*"
_SEATBELT_PRECASED_OVERRIDES: dict[str, str] = {
    _CREDENTIAL_TOKEN_FAMILY_PATTERNS[0]: _FA1_SEATBELT_PATTERN,
}


def _seatbelt_fold(pat: str) -> str:
    """Case-fold `pat` for splicing into a Seatbelt profile, using the precomputed
    override for the one pattern `_any_case` can't safely handle (see above)."""
    override = _SEATBELT_PRECASED_OVERRIDES.get(pat)
    return override if override is not None else _any_case(pat)


_READ_OPS = "file-read-data file-map-executable"
_WRITE_OPS = "file-write*"

# secreview-0110-fix2 finding 2: workspace._normalize_component strips trailing spaces
# and dots before matching a component against policy (`"secrets. "`/`"secrets."` can't
# dodge a match on `"secrets.yaml"`-style patterns), but Seatbelt matches the raw,
# unnormalized path -- so `credentials.json.`, `credentials.json ` (trailing space), and
# `.env ` all compiled to sandbox-ALLOW while the file tools already denied them (via the
# stripped form). `_DENY_TRAILING_SUFFIX` reproduces that stripping for DENY lines: a
# trailing run of dots/spaces/tabs before the anchor is tolerated, so the deny still
# fires on the raw name.
#
# A literal TAB is additionally tolerated on the deny side (but deliberately NOT the
# allow/exception side below): workspace.py's `resolve()` denies ANY path containing a
# control character outright, via `_has_control_chars`, before it even reaches component
# matching -- so a name like `credentials.json` + a trailing tab byte is *always* denied
# by the file tools, regardless of what it would normalize to. Tolerating a trailing tab
# on a DENY line only ever narrows the sandbox/file-tool gap (never widens it): it can't
# make the sandbox deny something the file tools would allow, because the file tools deny
# every tab-bearing path unconditionally.
#
# These are plain string literals with an actual tab character (not the two-char
# `\t` escape) embedded directly in Python source, so the byte that lands in the
# profile text is a literal 0x09 -- not a backslash followed by `t`, which POSIX bracket
# expressions do not treat as an escape.
_DENY_TRAILING_SUFFIX = "[. \t]*"
# The allow/exception side re-permits a handful of legitimate template/source names
# (`.env.example`, `secrets.py`, ...). Tolerating a trailing dot/space here mirrors the
# same stripping (so `.env.example.`/`.env.example ` stay allowed, matching what the file
# tools allow after normalization) -- but NOT a trailing tab: since the file tools deny
# every tab-bearing path unconditionally (see above), an allow line that tolerated a
# trailing tab would let the sandbox permit a read the file tools always refuse, which is
# exactly the gap this fix closes elsewhere. `[. ]*` (dot and space only) keeps the
# allow side no more permissive than the file tools ever are.
_ALLOW_TRAILING_SUFFIX = "[. ]*"


def _secret_name_denies(workspace: str, ops: str = _READ_OPS) -> list[str]:
    """Profile lines denying `ops` on secret-shaped names anywhere under the workspace.

    `ops` defaults to the read operations. `build_seatbelt_argv` also calls this with
    `_WRITE_OPS` ("file-write*"): without a write-side deny, a sandboxed `mv .env
    notes.md` (or `mv secrets.yaml s.md`) doesn't need to READ `.env`/`secrets.yaml` at
    all -- rename only needs write access to unlink/recreate the entry -- so it slips
    past the read-only deny, and the Read tool (which runs outside the sandbox, on the
    real filesystem) then serves the renamed plaintext.
    """
    # A quote or newline cannot sit inside the profile's regex literal: match it with `.`
    # instead (a slightly broader deny, never a narrower one).
    base = "".join("." if ch in '"\n\r' else re.escape(ch) for ch in workspace.rstrip("/"))
    lines = [
        f'(deny {ops} (regex #"^{base}/(.*/)?{_seatbelt_fold(pat)}{_DENY_TRAILING_SUFFIX}$"))'
        for pat in _SECRET_NAME_PATTERNS
    ]
    lines += [
        f'(allow {ops} (regex #"^{base}/(.*/)?{_seatbelt_fold(pat)}{_ALLOW_TRAILING_SUFFIX}$"))'
        for pat in _SECRET_NAME_EXCEPTIONS
    ]
    return lines


def _secret_dir_denies(workspace: str, ops: str = _READ_OPS) -> list[str]:
    """Profile lines denying `ops` anywhere beneath a secret-shaped directory component.

    Mirrors `_secret_dir_name_denied`'s three rules as static regex fragments (Seatbelt
    evaluates against paths that may be created during the sandboxed run, not just ones
    that exist at argv-build time, so this can't just be a pre-scan like
    `_secret_dirs_under`, the bwrap side): the exact `_SECRET_DIR_NAMES` alternation, the
    `secrets.`/`.secrets.` prefix (folded into `_GENERIC_SECRET_PATTERNS`, with no
    extension carve-out for a directory), every other GENERIC pattern, and
    `_SECRET_DIR_FAMILY_PATTERNS` (the credential/token family, last-segment-anchored so
    `credential_provider`/`credential-store` don't match while `aws_credentials`/
    `credentials.json` do). `(.*/)?` matches any prefix, each fragment matches one
    directory name, and `/.*` requires at least one more path segment after it -- the
    standalone directory entry itself, for every name here, is already covered by
    `_secret_name_denies`: every directory-denied name here also matches a
    `_SECRET_NAME_PATTERNS` fragment as a bare literal path (that's also what makes
    `_secret_name_denies` with `_WRITE_OPS` block renaming the directory entry itself,
    e.g. `mv secrets pkg`).

    `ops` defaults to the read operations; `build_seatbelt_argv` also calls this with
    `_WRITE_OPS` so a worker can't create new files inside a secret-shaped directory.
    """
    base = "".join("." if ch in '"\n\r' else re.escape(ch) for ch in workspace.rstrip("/"))
    # Not case-folded here: each fragment in `deny_fragments` (including this one) is
    # passed through `_any_case` once, below, when building the profile lines.
    exact = "(" + "|".join(re.escape(n) for n in _SECRET_DIR_NAMES) + ")"
    deny_fragments = [exact, *_GENERIC_SECRET_PATTERNS, *_SECRET_DIR_FAMILY_PATTERNS]
    # secreview-0110-fix2 finding 2: tolerate a trailing dot/space/tab on the DIRECTORY
    # component itself too, same reasoning as `_secret_name_denies` above -- a directory
    # named e.g. `secrets ` (trailing space) normalizes to `secrets` for the file tools
    # (`workspace._normalize_component`) and must be denied here as well.
    lines = [
        f'(deny {ops} (regex #"^{base}/(.*/)?{_seatbelt_fold(pat)}{_DENY_TRAILING_SUFFIX}/.*$"))'
        for pat in deny_fragments
    ]
    lines.append(
        f'(allow {ops} (regex #"^{base}/(.*/)?'
        f'{_any_case(_ENV_EXAMPLE_EXCEPTION_PATTERN)}{_ALLOW_TRAILING_SUFFIX}/.*$"))'
    )
    return lines


def _secret_files_under(
    workspace: Path, limit_entries: int = 50000, limit_hits: int = 500
) -> list[Path]:
    """Secret-named regular files under `workspace`: the bwrap side of `_secret_name_denies`.

    bubblewrap can only mount whole paths, never match names by regex, so the
    Seatbelt profile's name-based deny is reproduced at argv-build time:
    `build_bwrap_argv` shadows every hit with `--ro-bind /dev/null <file>`
    (later mounts win over the workspace bind). Matching mirrors Seatbelt:
    case-insensitive `re.fullmatch` of the final path component against
    `_SECRET_NAME_PATTERNS`, minus `_SECRET_NAME_EXCEPTIONS`.

    Only plain regular files are returned. Symlinks are skipped: Seatbelt
    matches the path a read resolves to, so the mask belongs on the target's
    name (a link named `.env` pointing at `notes.txt` reads fine on macOS too),
    and with `followlinks=False` symlinked directories are never descended.
    The walk never enters `.git` (git's own object store, not worker material).

    Limits cap the walk for huge workspaces / pathological hit counts: it
    stops after `limit_entries` directory entries or `limit_hits` hits.

    Files created after the sandbox starts are NOT covered -- the worker itself
    wrote those, and this scan runs once, before the sandbox is launched.
    """
    hits: list[Path] = []
    entries = 0
    for dirpath, dirnames, filenames in os.walk(workspace, followlinks=False):
        dirnames[:] = [d for d in dirnames if d != ".git"]
        entries += len(dirnames)
        if entries > limit_entries or len(hits) >= limit_hits:
            return hits
        for name in filenames:
            entries += 1
            if entries > limit_entries or len(hits) >= limit_hits:
                return hits
            path = Path(dirpath) / name
            if path.is_symlink() or not path.is_file():
                continue
            if any(p.fullmatch(name) for p in _SECRET_NAME_RES) and not any(
                p.fullmatch(name) for p in _SECRET_NAME_EXCEPTION_RES
            ):
                hits.append(path)
    return hits


def _secret_dirs_under(
    workspace: Path, limit_entries: int = 50000, limit_hits: int = 500
) -> list[Path]:
    """Directories under `workspace` for which `_secret_dir_name_denied` is True:
    the bwrap side of `_secret_dir_denies`.

    bwrap has no regex mount matching, so `build_bwrap_argv` shadows each hit
    with an empty `--tmpfs` mount, hiding the whole subtree the same way the
    Seatbelt regex does. A masked directory is not descended into -- its
    contents are already unreachable once the tmpfs mount lands, and this
    walk only needs the directory's own path. Matches `_secret_files_under`'s
    conventions: `.git` is skipped, symlinked directories are never entered
    (and never masked here -- a symlinked directory's target lives outside
    the workspace bind and is unreachable regardless), and the walk is capped
    by the same `limit_entries`/`limit_hits`.
    """
    hits: list[Path] = []
    entries = 0
    for dirpath, dirnames, filenames in os.walk(workspace, followlinks=False):
        entries += len(dirnames) + len(filenames)
        if entries > limit_entries or len(hits) >= limit_hits:
            return hits
        keep: list[str] = []
        for name in dirnames:
            if name == ".git":
                continue
            full = Path(dirpath) / name
            if full.is_symlink():
                keep.append(name)
                continue
            if _secret_dir_name_denied(name):
                hits.append(full)
                if len(hits) >= limit_hits:
                    return hits
            else:
                keep.append(name)
        dirnames[:] = keep
    return hits


def _probe_bwrap(bwrap_path: str) -> bool:
    try:
        proc = subprocess.run(
            # Same isolation flags as the real profile: where unprivileged user namespaces
            # are restricted, a bare `--ro-bind / /` probe passes while every real call fails.
            [
                bwrap_path,
                "--unshare-all",
                "--cap-drop",
                "ALL",
                "--die-with-parent",
                "--new-session",
                "--ro-bind",
                "/",
                "/",
                "--proc",
                "/proc",
                "--dev",
                "/dev",
                "--",
                "/bin/true",
            ],
            capture_output=True,
            timeout=_PROBE_TIMEOUT_S,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return proc.returncode == 0


def detect() -> SandboxKind | None:
    """Return the sandbox mechanism available and working on this machine, or None.

    macOS: `/usr/bin/sandbox-exec` exists and a trivial probe profile succeeds.
    Linux (and anything else): `bwrap` is on PATH and a trivial probe succeeds.
    """
    if sys.platform == "darwin":
        return "seatbelt" if _probe_seatbelt() else None
    bwrap_path = shutil.which("bwrap")
    if bwrap_path and _probe_bwrap(bwrap_path):
        return "bwrap"
    return None


def default_deny_read(state_dir: Path) -> list[Path]:
    """Directories/files a sandboxed worker must never be able to read.

    Under the deny-by-default profile these are already unreachable (nothing
    ever allows them), but the explicit denies stay: they're what actually
    matters if a future config change ever nests one of these under a path
    this module *does* allow (e.g. a toolchain home), and they close the gap
    for `bwrap`, where the base is `--ro-bind /` rather than deny-by-default.
    Credential stores, cloud CLI configs, shell history, and the plugin's own
    config/state (which could otherwise leak the OpenRouter key's config
    context or other workers' transcripts).
    """
    home = Path.home()
    return [
        home / ".ssh",
        home / ".aws",
        home / ".gnupg",
        home / ".config" / "gh",
        home / ".config" / "gcloud",
        home / ".azure",
        home / ".kube",
        home / ".docker",
        home / ".netrc",
        home / ".npmrc",
        home / ".pypirc",
        home / ".git-credentials",
        home / "Library" / "Keychains",
        home / ".claude",
        home / ".codex",
        home / ".config" / "anymodel-subagents",
        Path(state_dir),
        home / ".zsh_history",
        home / ".bash_history",
    ]


def _resolved_or_as_is(path: Path) -> Path:
    path = Path(path)
    return path.resolve() if path.exists() else path


@functools.cache
def _active_developer_dir() -> Path | None:
    """The developer dir `xcode-select -p` reports (macOS only); None elsewhere or on failure."""
    if sys.platform != "darwin":
        return None
    try:
        out = subprocess.run(
            ["/usr/bin/xcode-select", "-p"], capture_output=True, text=True, timeout=10, check=False
        )
    except (OSError, subprocess.SubprocessError):
        return None
    target = Path(out.stdout.strip()) if out.returncode == 0 and out.stdout.strip() else None
    return target if target is not None and target.is_dir() else None


def real_toolchain_bin() -> Path | None:
    """Directory holding the real git/python3/make on macOS, to put ahead of /usr/bin on PATH.

    The /usr/bin versions are shims that locate the toolchain through xcrun on every call.
    Inside the sandbox xcrun cannot write its lookup cache, which costs seconds per command
    (and tens of seconds under load). The real binaries need no lookup.
    """
    developer_dir = _active_developer_dir()
    if developer_dir is None:
        return None
    bin_dir = developer_dir / "usr" / "bin"
    return bin_dir if bin_dir.is_dir() else None


def build_seatbelt_argv(
    argv: list[str],
    *,
    workspace: Path,
    tmp: Path,
    deny_read: list[Path],
    extra_read: tuple[Path, ...] = (),
) -> list[str]:
    """Wrap `argv` to run under `sandbox-exec` with a deny-by-default profile.

    Base policy is `(deny default)`, not `(allow default)`: mach-lookup and
    network are denied outright with no exceptions (empirically, running
    python3/pytest/git/node/sh needed zero mach service allowances -- see
    module docstring), and file reads are denied except for the workspace,
    TMP, `extra_read` (e.g. a git worktree's real `.git` directories, see
    `bash.py`'s `compute_git_extra_read`), and a curated read-only
    system/toolchain allowlist. `file-read-metadata` (stat/realpath) is
    allowed everywhere so path traversal and existence checks don't
    themselves leak information asymmetrically -- but *listing* a directory
    outside the allowlist (`file-read-data` on the directory) still fails,
    so `ls`/`find` on a denied directory produce nothing.

    Paths derived from the environment or caller input (WORKSPACE, TMP,
    WORKSPACE_GIT, every `deny_read`/`extra_read` entry, and every per-user
    home-relative allowance) are passed as `-D NAME=value` parameters and
    referenced from the profile via `(param "NAME")` rather than
    string-interpolated into the profile text, so a workspace path
    containing spaces, quotes, or other profile-syntax characters can't
    inject into or break out of the profile. The hardcoded system
    directories in `_READ_ALLOW_SYSTEM_DIRS` are operator-controlled
    constants, never worker/environment input, so they're spliced directly.

    All paths are realpath'd first: Seatbelt matches literal (post-symlink)
    filesystem paths, and on macOS `/tmp` and `/var` are themselves symlinks
    into `/private/...` -- an unresolved path silently fails to match its own
    `(subpath ...)` rule, which was confirmed empirically against this
    profile (a write inside an unresolved workspace path was denied outright
    until the path was resolved).
    """
    ws = Path(workspace).resolve()
    tmp_resolved = Path(tmp).resolve()
    git_dir = ws / ".git"
    home = Path.home()

    params: list[str] = []

    def add_param(name: str, value: Path | str) -> str:
        params.extend(["-D", f"{name}={value}"])
        return name

    lines = [
        "(version 1)",
        "(deny default)",
        # No exceptions: verified empirically that python3, pytest, git,
        # node, and /bin/sh all run fine with mach-lookup fully denied.
        # This is also what closes the keychain/pasteboard/Apple
        # Events/LaunchServices exploits: SecKeychain, pbcopy/pbpaste, and
        # real (application-targeting) `osascript`/`open` calls all need a
        # mach service lookup first and fail closed without one.
        "(deny mach-lookup)",
        "(deny network*)",
        "(allow file-read-metadata)",
    ]

    read_lines = ["(allow file-read-data file-map-executable", '  (literal "/")']
    for d in _READ_ALLOW_SYSTEM_DIRS:
        read_lines.append(f'  (subpath "{d}")')
    for lit in _DEV_READ_LITERALS:
        read_lines.append(f'  (literal "{lit}")')
    read_lines.append('  (subpath "/dev/fd")')
    # Xcode is often installed under another name (Xcode_26.6.app on CI images, Xcode-beta.app),
    # and the /usr/bin/git|python3 shims dlopen libxcrun from whichever one is selected.
    read_lines.append('  (regex #"^/Applications/Xcode[^/]*\\.app(/|$)")')
    developer_dir = _active_developer_dir()
    if developer_dir is not None:
        name = add_param("DEVELOPER_DIR", developer_dir)
        read_lines.append(f'  (subpath (param "{name}"))')

    for i, rel in enumerate(_READ_ALLOW_HOME_RELATIVE_DIRS):
        name = add_param(f"HOMEDIR_{i}", _resolved_or_as_is(home / rel))
        read_lines.append(f'  (subpath (param "{name}"))')
    for i, rel in enumerate(_READ_ALLOW_HOME_FILES):
        name = add_param(f"HOMEFILE_{i}", _resolved_or_as_is(home / rel))
        read_lines.append(f'  (literal (param "{name}"))')
    read_lines.append(")")
    lines += read_lines

    lines += [
        "(allow file-write*",
        '  (subpath (param "WORKSPACE"))',
        '  (subpath (param "TMP"))',
        '  (literal "/dev/null")',
        '  (literal "/dev/tty")',
        '  (subpath "/dev/fd")',
        ")",
        # Overrides the allow above for .git specifically (last matching
        # rule wins in Seatbelt): hooks/config can't be planted via shell
        # even though .git lives under the otherwise-writable workspace.
        '(deny file-write* (subpath (param "WORKSPACE_GIT")))',
    ]
    params += [
        "-D",
        f"WORKSPACE={ws}",
        "-D",
        f"TMP={tmp_resolved}",
        "-D",
        f"WORKSPACE_GIT={git_dir}",
    ]

    for i, deny_path in enumerate(deny_read):
        name = f"DENY_READ_{i}"
        lines.append(f'(deny file-read* (subpath (param "{name}")))')
        # Verified empirically: a wildcard `file-read*` deny does NOT override an allow that
        # names `file-read-data` itself, whatever the order. Deny the named operations too.
        lines.append(f'(deny file-read-data file-map-executable (subpath (param "{name}")))')
        params += ["-D", f"{name}={_resolved_or_as_is(Path(deny_path))}"]

    # The workspace (a worktree under the state dir) and TMP can sit inside a read-denied
    # path; re-allow exactly those subtrees, plus any extra git directories a worktree
    # workspace needs (its real .git/worktrees/<id> dir and the repo's common .git dir --
    # see bash.py's compute_git_extra_read). Last matching rule wins.
    reallow = [
        "(allow file-read-data file-map-executable",
        '  (subpath (param "WORKSPACE"))',
        '  (subpath (param "TMP"))',
    ]
    for i, extra_path in enumerate(extra_read):
        name = f"EXTRA_READ_{i}"
        reallow.append(f'  (subpath (param "{name}"))')
        params += ["-D", f"{name}={_resolved_or_as_is(Path(extra_path))}"]
    reallow.append(")")
    lines += reallow

    # A denied path nested inside any re-allowed tree (a venv's pip.conf, under an
    # extra-read dir or under the workspace itself) must stay denied: the re-allow above
    # would otherwise win, being the later rule.
    extra_resolved = [
        str(ws),
        str(tmp_resolved),
        *(str(_resolved_or_as_is(Path(p))) for p in extra_read),
    ]
    for i, deny_path in enumerate(deny_read):
        denied = str(_resolved_or_as_is(Path(deny_path)))
        if any(denied == e or denied.startswith(e.rstrip("/") + "/") for e in extra_resolved):
            lines.append(
                f'(deny file-read-data file-map-executable (subpath (param "DENY_READ_{i}")))'
            )

    lines += _secret_name_denies(str(ws))
    lines += _secret_dir_denies(str(ws))
    # Write-side mirror of the read denies above (finding 2 of the 0110 fix review): a
    # sandboxed `mv`/`rm`/`ln` never needs to READ a secret-shaped name to rename, delete,
    # or recreate it, so the read-only denies above don't stop `mv .env notes.md` (or `mv
    # secrets pkg`) from moving the plaintext to an allowed name, where the Read tool --
    # which runs outside the sandbox -- then serves it. These come after the workspace
    # write-allow above, so they correctly narrow it (last matching rule wins).
    lines += _secret_name_denies(str(ws), ops=_WRITE_OPS)
    lines += _secret_dir_denies(str(ws), ops=_WRITE_OPS)

    lines += [
        "(allow process-fork)",
        "(allow process-exec)",
        # Needed for ordinary runtime behavior (CPU count, page size, etc.);
        # read-only system info disclosure, not a containment gap.
        "(allow sysctl-read)",
        # Needed empirically: `multiprocessing.Pool`/`Lock` (and
        # pytest-xdist) create named POSIX semaphores/shared memory
        # segments; without this, `_multiprocessing.SemLock(...)` raises
        # `PermissionError: [Errno 1] Operation not permitted`. These are
        # ephemeral, randomly-named, and local to the sandboxed process
        # tree -- not a meaningful exfiltration channel.
        "(allow ipc-posix-sem*)",
        "(allow ipc-posix-shm*)",
        # Needed empirically: without this, `multiprocessing.Pool` (and
        # anything else that calls `Process.terminate()`/`os.kill()` on its
        # own child) raises `PermissionError: [Errno 1] Operation not
        # permitted` from `os.kill()` deep in an atexit handler -- deny-by-
        # default denies *signal* just like everything else, where
        # `(allow default)` implicitly allowed it. The exception silently
        # swallowed by Python's atexit machinery then leaves the pool's
        # worker processes never told to stop, and the whole command hangs
        # until the outer Bash timeout kills it. Scoped to `children` (not
        # unscoped `signal`): a worker can signal its own descendants, never
        # an unrelated process on the machine.
        "(allow signal (target children))",
    ]
    profile = "\n".join(lines)
    return [_SEATBELT_EXE, *params, "-p", profile, *argv]


# ---------------------------------------------------------------------------
# bwrap (Linux). Cannot be exercised end-to-end on this macOS dev machine --
# no `bwrap` binary here -- so this is covered by argv-construction unit
# tests only. Treat it as UNTESTED END-TO-END: the general shape mirrors the
# Seatbelt policy (deny-by-default via explicit ro-binds rather than
# `--ro-bind / /`, workspace+tmp read-write, everything else unshared), but
# it has not been run against a real Linux toolchain the way the Seatbelt
# profile above was empirically validated against real python3/pytest/git/
# node invocations.
# ---------------------------------------------------------------------------

_BWRAP_RO_BIND_CANDIDATES: tuple[str, ...] = (
    "/usr",
    "/bin",
    "/sbin",
    "/lib",
    "/lib64",
    "/etc",
    "/opt",
    "/nix",
)

_BWRAP_HOME_RELATIVE_DIRS: tuple[str, ...] = (
    ".pyenv",
    ".local/share/uv",
    ".local/bin",
    ".cache/uv",
    ".cargo/bin",
    ".cargo/registry",
    ".rustup",
    "go/pkg",
    ".nvm",
    ".volta",
    ".asdf",
    ".local/share/mise",
    ".npm",
    ".gradle/caches",
    ".m2/repository",
    ".config/git",
)
_BWRAP_HOME_FILES: tuple[str, ...] = (".gitconfig",)


def build_bwrap_argv(
    argv: list[str],
    *,
    workspace: Path,
    tmp: Path,
    deny_read: list[Path],
    extra_read: tuple[Path, ...] = (),
) -> list[str]:
    """Wrap `argv` to run under `bwrap` with a policy equivalent to Seatbelt's.

    Deny-by-default via explicit `--ro-bind` of the system/toolchain
    directories that actually exist on this machine, rather than
    `--ro-bind / /` (which would make the entire filesystem readable).
    `--unshare-all` isolates network, PID, IPC, UTS, and user namespaces in
    one flag (replacing the previous `--unshare-net --unshare-pid` pair),
    `--cap-drop ALL` drops all Linux capabilities, `--die-with-parent` +
    `--new-session` prevent orphaning/detaching, and each existing
    `deny_read` path is shadowed with an empty `--tmpfs` mount rather than
    being made unreadable in place. Secret-shaped *file names* under the
    workspace are handled by `_secret_files_under` + `--ro-bind /dev/null`
    below, since bwrap cannot match mounts by regex the way Seatbelt can.
    """
    ws = Path(workspace).resolve()
    tmp_resolved = Path(tmp).resolve()
    bwrap_path = shutil.which("bwrap") or "bwrap"
    home = Path.home()

    cmd = [bwrap_path]

    for d in _BWRAP_RO_BIND_CANDIDATES:
        p = Path(d)
        if p.is_dir():
            cmd += ["--ro-bind", str(p), str(p)]
    for rel in _BWRAP_HOME_RELATIVE_DIRS:
        p = home / rel
        if p.is_dir():
            cmd += ["--ro-bind", str(p), str(p)]
    for rel in _BWRAP_HOME_FILES:
        p = home / rel
        if p.is_file():
            cmd += ["--ro-bind", str(p), str(p)]

    # Shadow denied paths first: the workspace (a worktree under the state dir) and TMP can sit
    # inside one, and later mounts win, so they must be bound after the tmpfs that hides it.
    for deny_path in deny_read:
        p = Path(deny_path)
        if p.is_dir():
            cmd += ["--tmpfs", str(p)]
        elif p.exists():
            cmd += ["--ro-bind", "/dev/null", str(p)]

    # /tmp first: later mounts win, so a workspace (or TMP) that itself lives under /tmp must
    # be bound after the private /tmp or it would be hidden by it. Found by the first real
    # bwrap run in CI, where pytest's tmp_path is under /tmp.
    cmd += [
        "--bind",
        str(tmp_resolved),
        "/tmp",
        "--bind",
        str(ws),
        str(ws),
        "--bind",
        str(tmp_resolved),
        str(tmp_resolved),
    ]

    for extra_path in extra_read:
        p = Path(extra_path)
        if p.exists():
            resolved = str(p.resolve())
            cmd += ["--ro-bind", resolved, resolved]

    git_dir = ws / ".git"
    if git_dir.exists():
        cmd += ["--ro-bind", str(git_dir), str(git_dir)]

    # Directory-shaped secret names (Seatbelt's `_secret_dir_denies` has no
    # bwrap equivalent): shadow each pre-existing `secrets`/`.secrets`/
    # `credentials`/`.credentials` directory under the workspace with an
    # empty tmpfs, hiding everything beneath it in one mount. Bound AFTER the
    # workspace mount -- later mounts win.
    secret_dirs = _secret_dirs_under(ws)
    for secret_dir in secret_dirs:
        cmd += ["--tmpfs", str(secret_dir)]

    # Name-based secret deny (Seatbelt's `_secret_name_denies` has no bwrap
    # equivalent): shadow each pre-existing secret-named regular file under the
    # workspace with /dev/null. Bound AFTER the workspace mount -- later mounts
    # win -- and before `--`, mirroring the deny_read FILE mask above. Skip
    # anything already hidden by a whole-directory tmpfs mask above (its
    # parent no longer exists in the new mount namespace once the tmpfs
    # lands, so a redundant file-level mount there is both unnecessary and
    # not guaranteed to succeed).
    for secret_file in _secret_files_under(ws):
        if any(secret_file == d or secret_file.is_relative_to(d) for d in secret_dirs):
            continue
        cmd += ["--ro-bind", "/dev/null", str(secret_file)]

    cmd += [
        "--unshare-all",
        "--cap-drop",
        "ALL",
        "--die-with-parent",
        "--new-session",
        "--dev",
        "/dev",
        "--proc",
        "/proc",
    ]
    cmd += ["--", *argv]
    return cmd


def wrap(
    argv: list[str],
    *,
    workspace: Path,
    tmp: Path,
    deny_read: list[Path],
    extra_read: tuple[Path, ...] = (),
    kind: SandboxKind | None = None,
) -> list[str]:
    """Return `argv` wrapped to run under the detected (or given) sandbox.

    `kind` defaults to `detect()`; callers that already know the kind (to
    avoid re-probing) or tests exercising one mechanism's argv construction
    on a machine that only has the other installed may pass it explicitly.
    """
    resolved_kind = kind or detect()
    if resolved_kind == "seatbelt":
        return build_seatbelt_argv(
            argv, workspace=workspace, tmp=tmp, deny_read=deny_read, extra_read=extra_read
        )
    if resolved_kind == "bwrap":
        return build_bwrap_argv(
            argv, workspace=workspace, tmp=tmp, deny_read=deny_read, extra_read=extra_read
        )
    raise PolicyError("Bash sandbox unavailable: no supported sandbox mechanism detected")
