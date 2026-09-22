"""Security-fix regression tests: credential/token/secret read-deny name families.

Directory-vs-file distinction (v0.1.10 usability regression, this fix): the
secret-NAME families above are matched per PATH COMPONENT, and used to apply
identically to every component -- denying whole directories like `token/`
(a standard Go/lexer package name), `internal/token/`, and
`credential_provider/` outright, even though only their FINAL component
(a file) is what the checks are meant to protect. The fix: a directory
component (every non-final component, and the final one when the path is an
existing directory) only denies on an EXACT match against `secrets`,
`.secrets`, `credentials`, `.credentials` (`_dir_component_denied` in
workspace.py; `_secret_dir_denies`/`_secret_dirs_under` in sandbox.py). The
final component, when it's a file (or doesn't exist yet), keeps the full
substring/bare-name/data-extension rules unchanged.

Three families must be denied by BOTH implementations -- the file-tool side
(`LocalWorkspace.is_denied`, via `_secret_name_denied`) and the sandbox-side
name patterns (`sandbox._SECRET_NAME_PATTERNS` minus
`_SECRET_NAME_EXCEPTIONS`, used by Seatbelt via `_secret_name_denies` and by
bwrap via `_secret_files_under`):

A. credential files: any name containing `credential` with no extension or a
   machine-config data extension.
B. token/secret files: `.token`/`.secret`/`.secrets` suffixes, bare
   `token`/`secrets`/`.secrets`, and `token`/`secrets` with a data extension.
C. `secrets.*`/`.secrets.*` (v0.1.10 regression): v0.1.9 denied every path
   component matching `secrets.*`; v0.1.10's `_secret_name_denied()` narrowed
   this to Family B's leaf/data-extension check, which let compound/backup/
   encrypted spellings (`secrets.sops.yaml`, `secrets.enc`, `secrets.yaml.bak`,
   `secrets.age`, `secrets.db`, `secrets.gpg`, `secrets.kdbx`,
   `secrets.tfvars`, ...) through. Family C denies any `secrets.`/`.secrets.`
   name unless its final extension is a genuine source/prose extension
   (`_SECRET_SOURCE_EXTS`): `secrets.py`/`secrets.md`/`secrets.test.ts` stay
   readable. This also flips bare `secrets.bak`, previously allowed under
   Family B's extension-denylist, to denied -- a deliberate tightening, not
   a regression: Family C uses an extension-allowlist instead.

The two implementations must agree on every name below; the final tests run
real Seatbelt `cat`s through `sandbox.wrap`.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

from anymodel_subagents.tools import Glob, Read, sandbox
from anymodel_subagents.tools.workspace import LocalWorkspace
from anymodel_subagents.types import PolicyError

DENIED_NAMES: tuple[str, ...] = (
    # A. credential files (no extension or a machine-config data extension).
    "credentials",
    "CREDENTIALS",
    "credentials.json",
    "credentials.yaml",
    "credentials.yml",
    "credentials.ini",
    "credentials.toml",
    "credentials.csv",
    "credentials.txt",
    "credentials.cfg",
    "credentials.conf",
    "credentials.env",
    "credentials.xml",
    "credentials.properties",
    "aws_credentials.csv",
    "credentials-prod.json",
    "gcp-credential.json",
    ".credentials",
    "CREDENTIALS.YAML",  # names are matched case-insensitively
    "credentials.backup.yaml",  # the real extension is still yaml
    # B. token/secret files.
    "token",
    "secrets",
    ".secrets",
    "token.txt",
    "secrets.yaml",
    "secrets.json",
    "app_secrets.json",
    "my-app-secrets.yaml",
    "deploy_token.ini",
    "deploy-token.toml",
    "api.token",
    "vault.secret",
    "prod.secrets",
    "TOKEN",
    "app.secrets.csv",
    # C. secrets.*/.secrets.* compound/encrypted/backup spellings.
    "secrets.sops.yaml",
    "secrets.enc",
    "secrets.yaml.bak",
    "secrets.age",
    "secrets.db",
    "secrets.gpg",
    "secrets.kdbx",
    "secrets.tfvars",
    ".secrets.bak",
    # Bare `secrets.bak` also flips to denied under Family C's
    # extension-allowlist (see module docstring).
    "secrets.bak",
    # Compound names with a trailing source/prose extension (this fix, finding 6):
    # the exception is exact `secrets.<ext>`/`.secrets.<ext>` only, not "ends with a
    # source extension" -- `secrets.test.ts` is a compound name, not a `.ts` file.
    "secrets.test.ts",
    "secrets.yaml.md",
    "secrets.env.py",
    "secrets.prod.rs",
    ".secrets.yaml.md",
    "secrets.json.rst",
    # LOW gaps closed by this fix (finding 5): sandbox previously allowed these
    # while the file tools already denied them.
    "app.keystore",
    "secret_key.rb",
    "credential-store",
    "credential_helpers",
)

ALLOWED_NAMES: tuple[str, ...] = (
    "credentials.py",
    "credentials.md",
    "credential_store.ts",
    "test_credentials.py",
    "CredentialForm.tsx",
    "credentials.bak",
    "tokenizer.py",
    "tokens.py",
    "token_utils.ts",
    "tokenizer.json",
    "package.json",
    "secrets.py",
    "secrets.md",
    "docs/secrets.md",
    "token.bak",
    "environment.py",
    "main.py",
)

# The data extensions shared by both implementations (pinned so a drift in one
# direction or the other is caught here).
DATA_EXTS = "json|yaml|yml|ini|toml|csv|txt|cfg|conf|env|xml|properties"

# secreview-0110-fix finding 4: every "anything" fragment is `[^/]*`, not `.*` --
# Seatbelt's regex engine matches `.` against `/` too, so a bare `.*` inside a
# filename-shaped fragment spans across path separators and over-denies an entire
# subtree (see sandbox.py's module comment above `_GENERIC_SECRET_PATTERNS`).
# finding 5: the credential family is two fragments, not one -- the single-regex form
# only matched `credential[s]` bare or immediately followed by an extension, missing
# extensionless compound names (`credential-store`, `credential_helpers`).
CREDENTIAL_PATTERNS = (
    # `\.?` first: os.path.splitext treats a single LEADING dot as a dotfile marker,
    # not an extension start, so `.credentials` must match this "no extension" fragment
    # too (workspace._secret_name_denied agrees: ext is "" for `.credentials`).
    r"\.?[^/.]*credentials?[^/.]*",
    rf"[^/]*credentials?[^/]*\.({DATA_EXTS})",
)
TOKEN_PATTERNS = (
    r"token",
    r"secrets",
    r"\.secrets",
    r"[^/]*\.(token|secret|secrets)",
    rf"([^/]*(\.|_|-))?token\.({DATA_EXTS})",
    rf"([^/]*(\.|_|-))?secrets\.({DATA_EXTS})",
)

# The source/prose extensions shared by both implementations for Family C.
SOURCE_EXTS = "py|pyi|ts|tsx|js|jsx|go|rs|rb|java|kt|md|rst"
SECRETS_PREFIX_DENY_PATTERNS = (r"\.?secrets\.[^/]*",)
# secreview-0110-fix finding 6: exact `secrets.<ext>`/`.secrets.<ext>` only -- no
# inner wildcard group (that was also a repeat of finding 1's `(?:...)` bug: Seatbelt's
# regex engine is POSIX ERE and has no non-capturing-group syntax).
SECRETS_PREFIX_EXCEPTION_PATTERNS = (rf"\.?secrets\.({SOURCE_EXTS})",)

# Directory-vs-file rule (this fix): paths where the secret-shaped name sits
# in a DIRECTORY component rather than the final (file) component.
DIR_ALLOWED_PATHS: tuple[str, ...] = (
    "token/token.go",
    "internal/token/scanner.go",
    "credential_provider/x.py",
    "credential-store/readme.md",
    "credential_helpers/x.py",
)
DIR_DENIED_PATHS: tuple[str, ...] = (
    "secrets/README.md",
    "credentials/prod.json",
    ".secrets/x",
    "token/token",
    "token.txt",
    "x/deploy_token.json",
    # secreview-0110-fix finding 3: the directory rule was too narrow -- these
    # became readable through 56b63d3 and must be denied again.
    "aws_credentials/default",
    "my_credentials/default",
    "credentials.json/notes.txt",
    "app-secrets.json/default",
    "deploy_token.json/default",
    "prod.secret/default",
    "secrets.yaml/prod",
    "secrets.d/prod",
)

# The exact directory names both implementations treat as secret-shaped
# (`_dir_component_denied`/`_SECRET_DIR_NAMES` in workspace.py,
# `_SECRET_DIR_NAMES` in sandbox.py).
SECRET_DIR_NAMES = frozenset({"secrets", ".secrets", "credentials", ".credentials"})


def _sandbox_name_denied(name: str) -> bool:
    """Mirror of `_secret_files_under`'s match: patterns minus exceptions."""
    if not any(p.fullmatch(name) for p in sandbox._SECRET_NAME_RES):
        return False
    return not any(p.fullmatch(name) for p in sandbox._SECRET_NAME_EXCEPTION_RES)


@pytest.fixture
def ws(tmp_path: Path) -> LocalWorkspace:
    root = tmp_path / "repo"
    root.mkdir()
    return LocalWorkspace(root=root)


@pytest.mark.parametrize("name", DENIED_NAMES)
def test_workspace_denies_credential_and_token_names(ws: LocalWorkspace, name: str) -> None:
    assert ws.is_denied(name) is True, name
    assert ws.is_denied(f"sub/dir/{name}") is True, f"sub/dir/{name}"


@pytest.mark.parametrize("name", ALLOWED_NAMES)
def test_workspace_allows_source_and_doc_lookalikes(ws: LocalWorkspace, name: str) -> None:
    full = ws.root / name
    full.parent.mkdir(parents=True, exist_ok=True)
    full.write_text("harmless")
    assert ws.is_denied(name) is False, name


@pytest.mark.parametrize("relpath", DIR_ALLOWED_PATHS)
def test_workspace_allows_files_in_lookalike_directories(ws: LocalWorkspace, relpath: str) -> None:
    """v0.1.10 usability regression: a secret-shaped SUBSTRING in a directory
    name (`token`, `credential_provider`) must not deny the whole subtree."""
    full = ws.root / relpath
    full.parent.mkdir(parents=True, exist_ok=True)
    full.write_text("harmless")
    assert ws.is_denied(relpath) is False, relpath


@pytest.mark.parametrize("relpath", DIR_DENIED_PATHS)
def test_workspace_denies_files_in_secret_named_directories_and_leaves(
    ws: LocalWorkspace, relpath: str
) -> None:
    """An exact secret-shaped DIRECTORY name (`secrets/`, `credentials/`,
    `.secrets/`) still denies everything beneath it, and a secret-shaped
    FILE name (`token.txt`, `deploy_token.json`, a file literally named
    `token`) still denies regardless of directory."""
    assert ws.is_denied(relpath) is True, relpath


def test_dir_component_denied_enterable_package_shapes() -> None:
    """`_dir_component_denied` (secreview-0110-fix finding 3): a directory whose name
    only matches the FILE rule because of the `credential` substring, or is bare
    `token`, stays enterable; every other secret-shaped directory name (the exact
    four, `secrets.*`/`.secrets.*`, or a credential/token/secrets name whose last
    `[._-]`-separated segment is itself credential(s)/secret(s)) is denied. `credential`
    (bare, singular) is a behavior change from the pre-fix "exact match only" rule: its
    own last segment IS `credential`, so it now denies too."""
    from anymodel_subagents.tools.workspace import _dir_component_denied

    for name in SECRET_DIR_NAMES:
        assert _dir_component_denied(name) is True, name
    for name in (
        "token",
        "credential_provider",
        "credential-store",
        "credential_helpers",
        "tokens",
    ):
        assert _dir_component_denied(name) is False, name
    for name in ("credential", "aws_credentials", "my_credentials", "credentials.json"):
        assert _dir_component_denied(name) is True, name


@pytest.mark.parametrize("name", DENIED_NAMES)
def test_sandbox_patterns_deny_credential_and_token_names(name: str) -> None:
    assert _sandbox_name_denied(name) is True, name


@pytest.mark.parametrize("name", ALLOWED_NAMES)
def test_sandbox_patterns_allow_source_and_doc_lookalikes(name: str) -> None:
    assert _sandbox_name_denied(name) is False, name


def test_sandbox_patterns_are_the_expected_families() -> None:
    """Pin that the sandbox table contains exactly the two new families
    (plus whatever predates them) with the specified data extensions."""
    for pattern in CREDENTIAL_PATTERNS + TOKEN_PATTERNS:
        assert pattern in sandbox._SECRET_NAME_PATTERNS
    for name in ALLOWED_NAMES:
        assert not any(re.fullmatch(p, name, re.IGNORECASE) for p in CREDENTIAL_PATTERNS)
        assert not any(re.fullmatch(p, name, re.IGNORECASE) for p in TOKEN_PATTERNS)


def test_sandbox_patterns_are_the_expected_secrets_prefix_family() -> None:
    """Pin Family C's deny/exception patterns and the source-extension set."""
    for pattern in SECRETS_PREFIX_DENY_PATTERNS:
        assert pattern in sandbox._SECRET_NAME_PATTERNS
    for pattern in SECRETS_PREFIX_EXCEPTION_PATTERNS:
        assert pattern in sandbox._SECRET_NAME_EXCEPTIONS
    for name in ("secrets.py", "secrets.md"):
        assert any(re.fullmatch(p, name, re.IGNORECASE) for p in SECRETS_PREFIX_EXCEPTION_PATTERNS)
    for name in (
        "secrets.bak",
        "secrets.yaml.bak",
        "secrets.sops.yaml",
        ".secrets.bak",
        # finding 6: a compound name is no longer exempted just because it ends in a
        # source extension.
        "secrets.test.ts",
        "secrets.yaml.md",
    ):
        assert any(re.fullmatch(p, name, re.IGNORECASE) for p in SECRETS_PREFIX_DENY_PATTERNS)
        assert not any(
            re.fullmatch(p, name, re.IGNORECASE) for p in SECRETS_PREFIX_EXCEPTION_PATTERNS
        )


def _sandbox_relpath_denied(relpath: str) -> bool:
    """Mirror of the combined Seatbelt decision for a whole relative path:
    the final-component name check (`_sandbox_name_denied`) OR'd with the
    directory rule (`sandbox._secret_dir_name_denied`, secreview-0110-fix finding 3)
    any non-final component gets."""
    parts = relpath.split("/")
    if _sandbox_name_denied(parts[-1]):
        return True
    return any(sandbox._secret_dir_name_denied(p) for p in parts[:-1])


@pytest.mark.parametrize("relpath", DIR_ALLOWED_PATHS)
def test_sandbox_allows_files_in_lookalike_directories(relpath: str) -> None:
    assert _sandbox_relpath_denied(relpath) is False, relpath


@pytest.mark.parametrize("relpath", DIR_DENIED_PATHS)
def test_sandbox_denies_files_in_secret_named_directories_and_leaves(relpath: str) -> None:
    assert _sandbox_relpath_denied(relpath) is True, relpath


def test_sandbox_secret_dir_names_match_workspace() -> None:
    """Pin sandbox._SECRET_DIR_NAMES to the same set workspace.py denies
    directories on (see workspace._SECRET_DIR_NAMES)."""
    assert {n.casefold() for n in sandbox._SECRET_DIR_NAMES} == SECRET_DIR_NAMES


def test_secret_dirs_under_masks_exact_and_broadened_names(tmp_path: Path) -> None:
    """Unit test for the bwrap side: `_secret_dirs_under` must hit the four
    exact-match directory names AND the broadened set from finding 3
    (`_secret_dir_name_denied`), not substring lookalikes, and must not descend
    into (or otherwise choke on) a masked directory's contents."""
    root = tmp_path / "ws"
    denied_dirs = (
        "secrets",
        ".secrets",
        "credentials",
        ".credentials",
        "aws_credentials",
        "credentials.json",
        "secrets.yaml",
    )
    for name in denied_dirs:
        (root / name).mkdir(parents=True)
        (root / name / "inside.txt").write_text("decoy")
    enterable_dirs = ("token", "credential_provider", "credential-store", "internal")
    for name in enterable_dirs:
        (root / name).mkdir(parents=True)
    hits = {p.relative_to(root).as_posix() for p in sandbox._secret_dirs_under(root)}
    assert hits == set(denied_dirs)


requires_seatbelt = pytest.mark.skipif(
    sandbox.detect() != "seatbelt",
    reason="requires a working Seatbelt (sandbox-exec) on this machine",
)


def _cat(path: Path, *, workspace: Path, tmp: Path) -> subprocess.CompletedProcess:
    argv = sandbox.wrap(
        ["/bin/cat", str(path)], workspace=workspace, tmp=tmp, deny_read=[], kind="seatbelt"
    )
    return subprocess.run(
        argv, capture_output=True, text=True, timeout=10, check=False, cwd=str(workspace)
    )


@requires_seatbelt
def test_seatbelt_blocks_credential_and_token_reads_but_not_source(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    tmp = tmp_path / "tmp"
    tmp.mkdir()

    for name, content in (
        ("aws_credentials.csv", "AWS-CREDENTIAL-DECOY"),
        ("deploy.token", "TOKEN-DECOY"),
        ("tokenizer.py", "TOKENIZER-ORDINARY"),
    ):
        (workspace / name).write_text(content)

    for name, decoy in (
        ("aws_credentials.csv", "AWS-CREDENTIAL-DECOY"),
        ("deploy.token", "TOKEN-DECOY"),
    ):
        proc = _cat(workspace / name, workspace=workspace, tmp=tmp)
        assert proc.returncode != 0, name
        assert "DECOY" not in proc.stdout, name

    control = _cat(workspace / "tokenizer.py", workspace=workspace, tmp=tmp)
    assert control.returncode == 0, control.stderr
    assert "TOKENIZER-ORDINARY" in control.stdout


@requires_seatbelt
def test_seatbelt_allows_token_dir_but_blocks_credentials_dir(tmp_path: Path) -> None:
    """Directory-vs-file rule under the real sandbox: a decoy under a
    `token/` directory (a legitimate Go/lexer package name) must stay
    readable, while the same decoy under `credentials/` must not."""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    tmp = tmp_path / "tmp"
    tmp.mkdir()

    (workspace / "token").mkdir()
    (workspace / "token" / "scanner.go").write_text("package token\n// TOKEN-PKG-DECOY")
    (workspace / "credentials").mkdir()
    (workspace / "credentials" / "prod.json").write_text('{"key": "PROD-CREDENTIAL-DECOY"}')

    ok = _cat(workspace / "token" / "scanner.go", workspace=workspace, tmp=tmp)
    assert ok.returncode == 0, ok.stderr
    assert "TOKEN-PKG-DECOY" in ok.stdout

    blocked = _cat(workspace / "credentials" / "prod.json", workspace=workspace, tmp=tmp)
    assert blocked.returncode != 0, blocked.stdout
    assert "PROD-CREDENTIAL-DECOY" not in blocked.stdout


async def test_read_and_glob_respect_directory_vs_file_secret_rule(ws: LocalWorkspace) -> None:
    """End-to-end regression for the whole-directory-deny bug: Read of a file
    under a `token/` directory succeeds and Glob lists it; the same shape
    under `credentials/` is refused and never listed."""
    (ws.root / "token").mkdir()
    (ws.root / "token" / "scanner.go").write_text("package token\n")
    (ws.root / "credentials").mkdir()
    (ws.root / "credentials" / "prod.json").write_text('{"key": "PROD-CREDENTIAL-DECOY"}')

    out = await Read().run({"file_path": "token/scanner.go"}, ws)
    assert "package token" in out

    with pytest.raises(PolicyError):
        await Read().run({"file_path": "credentials/prod.json"}, ws)

    go_hits = (await Glob().run({"pattern": "**/*.go"}, ws)).splitlines()
    assert "token/scanner.go" in go_hits

    json_hits = (await Glob().run({"pattern": "**/*.json"}, ws)).splitlines()
    assert "credentials/prod.json" not in json_hits


# secreview-0110-fix2 findings 1 & 2: fixture tree covering the previously-uncovered
# in-workspace `.aws`/`.ssh`/`.gnupg` directories (finding 1) and trailing dot/space
# names (finding 2), plus a set of names that must stay readable either way.
_PARITY_FIXTURE_DENIED: tuple[str, ...] = (
    ".aws/config",
    ".ssh/known_hosts",
    ".gnupg/x.conf",
    "credentials.json.",
    ".env ",
    "token.txt.",
)
_PARITY_FIXTURE_ALLOWED: tuple[str, ...] = (
    "token/scanner.go",
    "credential_provider/README.txt",
    "src/app.py",
    "secrets.py",
    ".env.example",
    "README.md",
    "Makefile",
    "tests/test_app.py",
    "docs/index.md",
    "package.json",
)


@requires_seatbelt
def test_seatbelt_matches_local_workspace_over_fixture_tree(tmp_path: Path) -> None:
    """secreview-0110-fix2 findings 1 & 2 parity check: for every path in the fixture
    tree above, the real Seatbelt decision (via `_cat`) must agree with
    `LocalWorkspace.is_denied`."""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    tmp = tmp_path / "tmp"
    tmp.mkdir()
    ws = LocalWorkspace(root=workspace)

    marker = "PARITY-DECOY-SECRET"
    all_paths = (*_PARITY_FIXTURE_DENIED, *_PARITY_FIXTURE_ALLOWED)
    for rel in all_paths:
        p = workspace / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(marker)

    for rel in all_paths:
        proc = _cat(workspace / rel, workspace=workspace, tmp=tmp)
        sandbox_denied = proc.returncode != 0 or marker not in proc.stdout
        workspace_denied = ws.is_denied(rel)
        assert sandbox_denied == workspace_denied, (
            rel,
            f"sandbox={'DENY' if sandbox_denied else 'ALLOW'}",
            f"file_tools={'DENY' if workspace_denied else 'ALLOW'}",
        )
        assert workspace_denied == (rel in _PARITY_FIXTURE_DENIED), rel


async def test_workspace_and_glob_deny_secrets_sops_yaml_decoy(ws: LocalWorkspace) -> None:
    """End-to-end regression for the v0.1.10 hole: a `secrets.sops.yaml`
    decoy must not be resolvable and must not appear in a Glob listing."""
    decoy = ws.root / "secrets.sops.yaml"
    decoy.write_text("SOPS-ENCRYPTED-DECOY")
    (ws.root / "keep.py").write_text("harmless")

    with pytest.raises(PolicyError):
        ws.resolve("secrets.sops.yaml")

    out = await Glob().run({"pattern": "*"}, ws)
    results = out.splitlines()
    assert "secrets.sops.yaml" not in results
    assert "keep.py" in results
