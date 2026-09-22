"""LocalWorkspace: confines a worker to one directory tree.

Threat model: `resolve()` is called for every file-tool operation on behalf of an
untrusted LLM worker. It must refuse anything that would let the worker read or
write outside `root`, or touch secrets inside `root`.

TOCTOU note: `resolve()` returns a path computed from a symlink-resolved
snapshot of the filesystem at call time. Between that call and the actual
open()/os.replace() in files.py, an attacker with concurrent filesystem access
(e.g. another process racing a symlink swap into a parent directory) could in
principle redirect the write. We mitigate but do not fully eliminate this:
files.py re-resolves the parent directory immediately before the atomic
os.replace() and refuses if the resolved parent has changed, and refuses to
write through a path component that is a symlink at open time. Full closure
would require O_NOFOLLOW + openat()-style path-component-by-component opens,
which the stdlib does not expose portably; this is a documented residual risk.
"""

from __future__ import annotations

import fnmatch
import os
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path

from anymodel_subagents.types import PolicyError

# Names/patterns denied on ANY path component, matched case-insensitively.
# `.env.example`/`.env.sample`/`.env.template` are carved out before this list
# is consulted -- but per component (see _component_denied), so the carve-out
# never un-denies anything beneath a directory that happens to be so named.
_DENIED_COMPONENT_PATTERNS = [
    ".env",
    ".env.*",
    "*.env",
    "*.env.*",
    "*.pem",
    "*.key",
    "*.p12",
    "*.pfx",
    "id_rsa*",
    "id_dsa*",
    "id_ed25519*",
    "id_ecdsa*",
    "*_rsa",
    "authorized_keys",
    ".npmrc",
    ".pypirc",
    ".netrc",
    ".git-credentials",
    ".aws",
    ".ssh",
    ".gnupg",
    "*.keystore",
    "*.jks",
    "*.ovpn",
    "secret_key*",
]

_ALLOWED_ENV_EXCEPTIONS = {".env.example", ".env.sample", ".env.template"}

# The machine-config extensions where credential/token data actually lives.
# `credentials.py`/`credentials.md`/`token_utils.ts` (prose, code) stay
# readable; `aws_credentials.csv`/`secrets.yaml` do not. See
# `_secret_name_denied()` and the parallel regex table in sandbox.py.
_SECRET_DATA_EXTS = {
    "json",
    "yaml",
    "yml",
    "ini",
    "toml",
    "csv",
    "txt",
    "cfg",
    "conf",
    "env",
    "xml",
    "properties",
}

# Secret-shaped DIRECTORY names: an exact (normalized) match denies the whole
# subtree beneath it. Deliberately narrower than the FILE-shaped secret-name
# checks in `_secret_name_denied()` -- no substring match (`credential`) and
# no bare/data-extension leaf check -- so ordinary directories that happen to
# contain or start with these words (`token/` -- a standard Go/lexer package
# name --, `credential_provider/`, `credential-store/`) stay enterable. See
# `_dir_component_denied()`.
_SECRET_DIR_NAMES = frozenset({"secrets", ".secrets", "credentials", ".credentials"})

# When the FILE rule (`_secret_name_denied`) fires solely because of the
# `credential` substring, a directory of that name stays enterable unless its
# last `[._-]`-separated segment (extension stripped) is itself one of these
# words -- see `_dir_component_denied`.
_CREDENTIAL_DIR_ENTERABLE_EXCLUDED = frozenset({"credential", "credentials", "secret", "secrets"})

# Genuine source/prose extensions: the only things a `secrets.*`/`.secrets.*`
# name is allowed to be. Everything else under that stem (data, backup,
# archive, or encrypted spellings -- `secrets.yaml.bak`, `secrets.sops.yaml`,
# `secrets.enc`, `secrets.age`, `secrets.db`, `secrets.gpg`, `secrets.kdbx`,
# `secrets.tfvars`, ...) is denied. See `_secret_name_denied()`.
_SECRET_SOURCE_EXTS = {
    "py",
    "pyi",
    "ts",
    "tsx",
    "js",
    "jsx",
    "go",
    "rs",
    "rb",
    "java",
    "kt",
    "md",
    "rst",
}

# ---------------------------------------------------------------------------
# Write-policy tables.
#
# These two lists are the deny-vs-flag line. `_WRITE_DENIED_GLOBS` is
# everything a code-editing worker never legitimately needs to write: CI/CD
# pipeline definitions, editor/agent configuration, and shell/interpreter
# startup files that execute unreviewed the moment they land (in a
# maintainer's next shell session, in CI, on next `python -c`). A worker that
# "needs" to touch one of these is almost always doing something the task
# didn't ask for, so these are refused outright.
#
# `_SENSITIVE_GLOBS` is the opposite case: files a worker may legitimately
# need to edit as part of ordinary source changes (build config, test
# config, lockfiles, Dockerfiles) but that execute later, outside the
# current review — so the write is allowed, but `is_sensitive()` flags it
# for deliberate human review rather than letting it blend in with ordinary
# .py/.ts edits.
#
# Entries ending in "/**" are denied/flagged at any depth (not just root);
# bare names/globs are matched against any single path component as well as
# the full root-relative path, so those also apply at any depth.
# ---------------------------------------------------------------------------

_WRITE_DENIED_GLOBS = [
    # Agent configuration and CI-adjacent worker plumbing.
    ".github/**",  # all of it: workflows, CODEOWNERS, actions, issue templates
    ".claude/**",
    ".codex/**",
    ".mcp.json",
    ".workers/**",
    "CLAUDE.md",
    "AGENTS.md",
    # Virtualenvs: a worker never needs to write one, and a planted `.venv/bin/python` would
    # run unsandboxed the next time someone runs the project's tests (and sandboxed Bash
    # puts a workspace `.venv` first on PATH).
    ".venv/**",
    "venv/**",
    # Shell / direnv startup files: sourced automatically by a future shell.
    ".envrc",
    ".bashrc",
    ".zshrc",
    ".zshenv",  # zsh sources this unconditionally, even non-interactive
    ".zprofile",
    ".profile",
    ".bash_profile",
    ".kshrc",
    ".bash_logout",
    # Editor config that executes code (modelines, plugins, autocommands).
    ".vimrc",
    # Editor / IDE workspace settings (can carry auto-run tasks/extensions).
    ".vscode/**",
    ".idea/**",
    ".devcontainer/**",
    # CI/CD pipeline definitions across common providers.
    ".pre-commit-config.yaml",
    ".gitattributes",
    ".gitmodules",
    ".gitlab-ci.yml",
    ".gitlab/**",  # child pipelines and shared CI config live here
    ".circleci/**",
    ".travis.yml",
    "Jenkinsfile",
    "azure-pipelines.yml",
    "bitbucket-pipelines.yml",
    ".buildkite/**",
    ".drone.yml",
    "appveyor.yml",
    ".appveyor.yml",
    ".woodpecker.yml",
    ".woodpecker/**",
    "cloudbuild.yaml",
    ".gitpod.yml",
    "codemagic.yaml",
    ".teamcity/**",
    ".husky/**",
    ".githooks/**",  # git core.hooksPath equivalent of .husky
    # Build-tool entrypoints that fetch and run code on the user's machine.
    "gradlew",
    "gradlew.bat",
    "gradle/wrapper/gradle-wrapper.properties",
    # Python import-time hooks: run on every interpreter start in this env.
    "sitecustomize.py",
    "usercustomize.py",
    "*.pth",
    # Other assistants'/tools' agent-config equivalents of CLAUDE.md/.claude.
    ".cursor/**",
    ".cursorrules",
    ".windsurfrules",
    ".clinerules",
    ".aider*",
    ".roo/**",
    ".gemini/**",
    "GEMINI.md",
    "copilot-instructions.md",
    ".claude-plugin/**",
]

_SENSITIVE_GLOBS = [
    "conftest.py",
    "Makefile",
    "makefile",
    "GNUmakefile",
    "justfile",
    "package.json",
    "pyproject.toml",
    "setup.py",
    "setup.cfg",
    "Dockerfile*",
    "docker-compose*.y*ml",
    "tox.ini",
    "noxfile.py",
    "build.gradle*",
    "pom.xml",
    "Cargo.toml",
    "build.rs",
    "go.mod",
    # Shell-family scripts beyond .sh: all execute outside the current review.
    "*.sh",
    "*.bash",
    "*.zsh",
    "*.ksh",
    "*.fish",
    "*.ps1",
    "*.bat",
    "*.cmd",
    # JS/TS build configs (webpack/vite/rollup/jest/next/gulp/...).
    "*.config.js",
    "*.config.ts",
    "*.config.mjs",
    "*.config.cjs",
    # Other build entrypoints.
    "binding.gyp",
    "Vagrantfile",
    "meson.build",
    "SConstruct",
    "build.xml",
    "requirements*.txt",
    "Gemfile",
    "Rakefile",
    "CMakeLists.txt",
    "package-lock.json",
    "yarn.lock",
    "pnpm-lock.yaml",
    "uv.lock",
    "poetry.lock",
    "Cargo.lock",
]


def _normalize_component(name: str) -> str:
    """Normalize one path component for policy matching.

    Unicode-normalizes to NFC and casefolds so lookalike encodings and case
    variants collapse to the same string, and strips trailing dots/spaces so
    `"secrets. "` / `"secrets."` can't dodge a match on `"secrets.yaml"`-style
    patterns (a classic Windows-alias trick; cheap to close on POSIX too).
    """
    return unicodedata.normalize("NFC", name).casefold().rstrip(" .")


def _normalize_pattern(pattern: str) -> str:
    return unicodedata.normalize("NFC", pattern).casefold()


def _has_control_chars(s: str) -> bool:
    return any(ord(ch) < 32 or ord(ch) == 127 for ch in s)


def _secret_name_denied(norm: str) -> bool:
    """Credential- and token/secret-shaped final components.

    Family A: any name containing `credential` with no extension or one of
    `_SECRET_DATA_EXTS` (so `aws_credentials.csv` and `credentials.backup.yaml`
    are denied while `credentials.py`/`credentials.md` stay readable).

    Family B: `token`/`secrets`/`.secrets` bare, `*.token`/`*.secret`/`*.secrets`,
    and `token`/`secrets`/`*_secrets`/`*-secrets`/`*_token`/`*-token` with a data
    extension (`token.txt` denied, `tokenizer.py`/`secrets.py` readable).

    Family C: any name starting with `secrets.` or `.secrets.` -- Family B's
    extension-denylist misses compound/encrypted/backup spellings
    (`secrets.sops.yaml`, `secrets.enc`, `secrets.yaml.bak`, `secrets.age`,
    `secrets.db`, `secrets.gpg`, `secrets.kdbx`, `secrets.tfvars`, ...), so
    these are denied unless the name is *exactly* `secrets.<ext>` /
    `.secrets.<ext>` for a single genuine source/prose extension
    (`_SECRET_SOURCE_EXTS`): `secrets.py`, `secrets.md` stay readable, but a
    compound name (`secrets.test.ts`, `secrets.yaml.md`) is denied -- the
    exemption is for a source *file*, not for anything with a trailing
    source-looking extension.

    Must agree with `_SECRET_NAME_PATTERNS` in sandbox.py; see
    tests/test_secfix_secret_names.py, which pins the two implementations
    to the same decisions.
    """
    root, dot_ext = os.path.splitext(norm)
    ext = dot_ext.lstrip(".")
    if "credential" in norm:
        return not ext or ext in _SECRET_DATA_EXTS
    if norm in ("token", "secrets", ".secrets"):
        return True
    if dot_ext in (".token", ".secret", ".secrets"):
        return True
    if norm.startswith(("secrets.", ".secrets.")):
        prefix = "secrets." if norm.startswith("secrets.") else ".secrets."
        rest = norm[len(prefix) :]
        return rest not in _SECRET_SOURCE_EXTS
    if ext and ext in _SECRET_DATA_EXTS:
        # The last name segment before the extension (separators: _ - .) is
        # what counts, so `tokenizer.json` stays readable while
        # `app-secrets.json`/`deploy_token.json` are denied.
        leaf = root.replace("-", ".").replace("_", ".").rsplit(".", 1)[-1]
        return leaf in ("token", "secrets")
    return False


def _component_denied(name: str) -> bool:
    """Policy for a FINAL path component that is a file (or doesn't exist yet).

    Full rules: `_secret_name_denied()`'s substring/bare-name/data-extension
    checks, plus `_DENIED_COMPONENT_PATTERNS`. See `_dir_component_denied()`
    for the narrower rule applied to every other (directory) component.
    """
    norm = _normalize_component(name)
    if norm in _ALLOWED_ENV_EXCEPTIONS:
        return False
    return _secret_name_denied(norm) or any(
        fnmatch.fnmatch(norm, pat) for pat in _DENIED_COMPONENT_PATTERNS
    )


def _credential_or_token_dir_is_enterable(norm: str) -> bool:
    """True when `_secret_name_denied(norm)` fired for a shape that's still a
    legitimate directory/package name: bare `token`, or a name containing
    `credential` whose LAST `[._-]`-separated segment (the name's root, with
    any extension stripped) is not itself `credential`/`credentials`/
    `secret`/`secrets`.

    So `token/`, `credential_provider/`, `credential-store/`,
    `credential_helpers/` stay enterable, while `aws_credentials/`,
    `my_credentials/`, `credentials.json/`, `prod.secret/` do not -- the
    caller only consults this when `_secret_name_denied(norm)` is already
    True, so it never needs to re-derive *why* the bare/token/data-extension
    branches denied a name: those never leave a `credential`/`credentials`/
    `secret`/`secrets` trailing segment for a name that isn't already exact
    matched by `_SECRET_DIR_NAMES`.
    """
    if norm == "token":
        return True
    if "credential" not in norm:
        return False
    root, _ext = os.path.splitext(norm)
    last_segment = re.split(r"[._-]", root)[-1] if root else norm
    return last_segment not in _CREDENTIAL_DIR_ENTERABLE_EXCLUDED


def _dir_component_denied(name: str) -> bool:
    """Policy for a path component that is a DIRECTORY (every non-final
    component, and the final one when the resolved path is an existing
    directory).

    Denies when: (a) the name exactly matches `_SECRET_DIR_NAMES`; (b) the
    name starts with `secrets.` or `.secrets.` (a directory is never a
    genuine source file, so -- unlike the FILE rule's Family C -- there is no
    source-extension carve-out here: `secrets.py/` is still denied); or (c)
    `_secret_name_denied(name)` is True and the name isn't one of the
    enterable package shapes from `_credential_or_token_dir_is_enterable`
    (so `token/token.go`, `internal/token/scanner.go`, and
    `credential_provider/x.py` stay readable, while `aws_credentials/`,
    `credentials.json/`, `app-secrets.json/`, `deploy_token.json/`,
    `prod.secret/` still deny everything beneath them).
    `_DENIED_COMPONENT_PATTERNS` (.ssh, .aws, .gnupg, .env*, key files, ...)
    applies exactly as it does to a file component -- those are never
    legitimate directory names either.
    """
    norm = _normalize_component(name)
    if norm in _ALLOWED_ENV_EXCEPTIONS:
        return False
    if norm in _SECRET_DIR_NAMES:
        return True
    if norm.startswith(("secrets.", ".secrets.")):
        return True
    if _secret_name_denied(norm) and not _credential_or_token_dir_is_enterable(norm):
        return True
    return any(fnmatch.fnmatch(norm, pat) for pat in _DENIED_COMPONENT_PATTERNS)


def _matches_any_depth(relparts: tuple[str, ...], patterns: list[str]) -> bool:
    norm_parts = [_normalize_component(p) for p in relparts]
    joined = "/".join(norm_parts)
    for pat in patterns:
        pat_n = _normalize_pattern(pat)
        if pat_n.endswith("/**"):
            prefix_parts = pat_n[: -len("/**")].split("/")
            n = len(prefix_parts)
            for i in range(len(norm_parts) - n + 1):
                if norm_parts[i : i + n] == prefix_parts:
                    return True
        else:
            if fnmatch.fnmatch(joined, pat_n):
                return True
            if any(fnmatch.fnmatch(p, pat_n) for p in norm_parts):
                return True
    return False


def _write_denied(relparts: tuple[str, ...]) -> bool:
    return _matches_any_depth(relparts, _WRITE_DENIED_GLOBS)


def _is_sensitive_path(relparts: tuple[str, ...]) -> bool:
    return _matches_any_depth(relparts, _SENSITIVE_GLOBS)


@dataclass
class LocalWorkspace:
    """Confines a worker to `root`. Implements the Workspace protocol."""

    root: Path

    def __post_init__(self) -> None:
        self.root = Path(self.root).resolve()

    def is_denied(self, path: str | Path) -> bool:
        """True if `path` would be refused by policy for reads. Never raises."""
        try:
            self.resolve(str(path), for_write=False)
        except PolicyError:
            return True
        except (OSError, ValueError):
            return True
        return False

    def is_write_denied(self, path: str | Path) -> bool:
        """True if writing to `path` would be refused by policy. Never raises.

        Runs the exact same normalization/resolution as `resolve(..., for_write=True)`
        -- write-denied globs, secret-shaped names (`.env`, keys, credential
        files), `.git` internals, path escape, and writing through a symlink
        -- so callers that only need a yes/no answer (e.g. worktree
        finalization scanning a sandboxed script's changes) don't have to
        duplicate that logic or catch `PolicyError` themselves. Unlike
        `resolve()`, this never raises and does not require `path` to exist.
        """
        try:
            self.resolve(str(path), for_write=True)
        except PolicyError:
            return True
        except (OSError, ValueError):
            return True
        return False

    def is_sensitive(self, path: str | Path) -> bool:
        """True if a write to `path` is allowed but should be flagged for review.

        This does not consult the deny lists or re-run `resolve()` — it is
        meant to be called on a path that has already been resolved and
        allowed for write, purely to classify it for reporting. A path
        outside `root` classifies as not sensitive (it isn't this
        workspace's concern).
        """
        p = Path(path)
        try:
            rel = p.relative_to(self.root) if p.is_absolute() else p
        except ValueError:
            return False
        return _is_sensitive_path(rel.parts)

    def resolve(self, path: str, *, for_write: bool = False) -> Path:
        if path is None:
            raise PolicyError("invalid path")
        if _has_control_chars(path):
            raise PolicyError("invalid path: contains control characters")
        stripped = path.strip()
        if not stripped:
            raise PolicyError("invalid path")

        raw = Path(path)
        candidate = raw if raw.is_absolute() else (self.root / raw)

        try:
            # Captured before resolving symlinks: is the exact requested path
            # itself a symlink (regardless of how its ancestors were
            # reached)? lstat only dereferences ancestor components, not the
            # final one, so this is safe even when parent directories are
            # themselves reached via symlinks.
            candidate_is_symlink = candidate.is_symlink()

            resolved, remainder = self._resolve_existing_and_remainder(candidate)
        except OSError as exc:
            raise PolicyError(f"invalid path: {exc.strerror or exc}") from exc

        if remainder:
            for part in remainder:
                if part in ("..", "."):
                    raise PolicyError("invalid path")
            final = resolved.joinpath(*remainder)
        else:
            final = resolved

        try:
            root_resolved = self.root.resolve()
        except OSError as exc:
            raise PolicyError(f"invalid path: {exc.strerror or exc}") from exc
        if not final.is_relative_to(root_resolved):
            raise PolicyError("path escapes workspace root")

        rel = final.relative_to(root_resolved)
        relparts = rel.parts

        # Deny .git anywhere in the path (reads and writes both).
        if any(_normalize_component(p) == ".git" for p in relparts):
            raise PolicyError("access to .git is denied")

        # Sensitive-name denial on any component (reads and writes). The
        # FINAL component -- when it resolves to a file, or doesn't exist yet
        # (a Write/Edit target) -- gets the full file-shaped rule
        # (`_component_denied`). Every other component -- and the final one
        # when the path is an EXISTING directory -- gets the narrower
        # directory rule (`_dir_component_denied`): see that function's
        # docstring for why (`token/`, `credential_provider/` vs. `secrets/`,
        # `credentials/`).
        final_is_existing_dir = not remainder and final.is_dir()
        last_index = len(relparts) - 1
        for i, part in enumerate(relparts):
            is_final_file_component = i == last_index and not final_is_existing_dir
            denied = (
                _component_denied(part) if is_final_file_component else _dir_component_denied(part)
            )
            if denied:
                raise PolicyError("access to this file is denied by policy")

        if for_write:
            if _write_denied(relparts):
                raise PolicyError("writing to this path is denied by policy")
            # Refuse to write through a symlink: if the exact requested path
            # itself is a symlink (even though `final` above is its fully
            # resolved, dereferenced target), deny outright.
            if candidate_is_symlink:
                raise PolicyError("refusing to write through a symlink")

        return final

    @staticmethod
    def _resolve_existing_and_remainder(candidate: Path) -> tuple[Path, tuple[str, ...]]:
        """Resolve symlinks on the longest existing prefix of `candidate`.

        Returns (resolved_existing_ancestor_or_full_path, remainder_parts_not_yet_existing).
        Using a single os.path.realpath() call on the existing prefix resolves
        arbitrarily long symlink chains in one shot. May raise OSError (e.g.
        ENAMETOOLONG on a pathological name); the caller converts that to
        PolicyError.
        """
        parts = candidate.parts
        for i in range(len(parts), 0, -1):
            prefix = Path(*parts[:i])
            if prefix.exists():
                real = Path(os.path.realpath(prefix))
                remainder = parts[i:]
                return real, remainder
        return Path(os.path.realpath(candidate)), ()
