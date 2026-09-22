"""Escape/policy tests for LocalWorkspace.resolve()."""

from __future__ import annotations

import os
import unicodedata
from pathlib import Path

import pytest

from anymodel_subagents.tools.workspace import LocalWorkspace
from anymodel_subagents.types import PolicyError


@pytest.fixture
def ws(tmp_path: Path) -> LocalWorkspace:
    root = tmp_path / "repo"
    root.mkdir()
    return LocalWorkspace(root=root)


def test_relative_path_resolves_inside_root(ws: LocalWorkspace) -> None:
    (ws.root / "a.txt").write_text("hi")
    assert ws.resolve("a.txt") == ws.root / "a.txt"


def test_absolute_path_inside_root_resolves(ws: LocalWorkspace) -> None:
    (ws.root / "a.txt").write_text("hi")
    assert ws.resolve(str(ws.root / "a.txt")) == ws.root / "a.txt"


def test_dotdot_traversal_outside_root_denied(ws: LocalWorkspace) -> None:
    with pytest.raises(PolicyError):
        ws.resolve("../outside.txt")


def test_dotdot_that_stays_inside_root_allowed(ws: LocalWorkspace) -> None:
    (ws.root / "sub").mkdir()
    (ws.root / "a.txt").write_text("hi")
    assert ws.resolve("sub/../a.txt") == ws.root / "a.txt"


def test_absolute_path_outside_root_denied(tmp_path: Path, ws: LocalWorkspace) -> None:
    outside = tmp_path / "elsewhere.txt"
    outside.write_text("nope")
    with pytest.raises(PolicyError):
        ws.resolve(str(outside))


def test_symlink_file_pointing_outside_denied(tmp_path: Path, ws: LocalWorkspace) -> None:
    outside = tmp_path / "secret.txt"
    outside.write_text("secret")
    link = ws.root / "link.txt"
    link.symlink_to(outside)
    with pytest.raises(PolicyError):
        ws.resolve("link.txt")


def test_symlinked_directory_pointing_outside_denied(tmp_path: Path, ws: LocalWorkspace) -> None:
    outside_dir = tmp_path / "outside_dir"
    outside_dir.mkdir()
    (outside_dir / "f.txt").write_text("x")
    link_dir = ws.root / "linked"
    link_dir.symlink_to(outside_dir)
    with pytest.raises(PolicyError):
        ws.resolve("linked/f.txt")


def test_symlink_chain_escaping_root_denied(tmp_path: Path, ws: LocalWorkspace) -> None:
    outside = tmp_path / "final_target.txt"
    outside.write_text("x")
    link2 = tmp_path / "link2.txt"
    link2.symlink_to(outside)
    link1 = ws.root / "link1.txt"
    link1.symlink_to(link2)
    with pytest.raises(PolicyError):
        ws.resolve("link1.txt")


def test_symlink_chain_staying_inside_root_allowed(ws: LocalWorkspace) -> None:
    target = ws.root / "real.txt"
    target.write_text("x")
    link2 = ws.root / "link2.txt"
    link2.symlink_to(target)
    link1 = ws.root / "link1.txt"
    link1.symlink_to(link2)
    assert ws.resolve("link1.txt") == target


def test_write_nonexistent_nested_path_under_symlinked_dir_inside_root(
    ws: LocalWorkspace,
) -> None:
    real_subdir = ws.root / "real_subdir"
    real_subdir.mkdir()
    linked = ws.root / "linked"
    linked.symlink_to(real_subdir)
    final = ws.resolve("linked/new/nested.txt", for_write=True)
    assert final == real_subdir / "new" / "nested.txt"


def test_write_nonexistent_nested_path_under_symlinked_dir_outside_root_denied(
    tmp_path: Path, ws: LocalWorkspace
) -> None:
    outside_dir = tmp_path / "outside_dir"
    outside_dir.mkdir()
    linked = ws.root / "linked"
    linked.symlink_to(outside_dir)
    with pytest.raises(PolicyError):
        ws.resolve("linked/new/nested.txt", for_write=True)


def test_root_itself_a_symlink_works(tmp_path: Path) -> None:
    real_root = tmp_path / "real_root"
    real_root.mkdir()
    (real_root / "a.txt").write_text("hi")
    link_root = tmp_path / "link_root"
    link_root.symlink_to(real_root)
    ws = LocalWorkspace(root=link_root)
    assert ws.root == real_root.resolve()
    assert ws.resolve("a.txt") == real_root.resolve() / "a.txt"


def test_nul_byte_denied(ws: LocalWorkspace) -> None:
    with pytest.raises(PolicyError):
        ws.resolve("a\x00.txt")


def test_empty_path_denied(ws: LocalWorkspace) -> None:
    with pytest.raises(PolicyError):
        ws.resolve("")
    with pytest.raises(PolicyError):
        ws.resolve("   ")


@pytest.mark.parametrize(
    "name",
    [
        ".env",
        ".ENV",
        ".Env",
        ".env.local",
        ".env.production",
        "id_rsa",
        "id_rsa.pub",
        "id_ed25519",
        "id_ecdsa",
        "server.pem",
        "cert.PEM",
        "priv.key",
        "bundle.p12",
        "bundle.pfx",
        ".npmrc",
        ".pypirc",
        ".netrc",
        ".aws",
        ".ssh",
        ".gnupg",
        "credentials.json",
        "secrets.yaml",
        "Secrets.yaml",
        "app.keystore",
    ],
)
def test_denied_names_in_any_component_case_insensitive(ws: LocalWorkspace, name: str) -> None:
    with pytest.raises(PolicyError):
        ws.resolve(name)
    # also denied nested under a subdirectory
    with pytest.raises(PolicyError):
        ws.resolve(f"sub/dir/{name}")


@pytest.mark.parametrize("name", [".env.example", ".env.sample", ".env.template"])
def test_env_example_variants_allowed(ws: LocalWorkspace, name: str) -> None:
    (ws.root / name).write_text("SAMPLE=1")
    assert ws.resolve(name) == ws.root / name


def test_git_internals_denied_for_read_and_write(ws: LocalWorkspace) -> None:
    git_dir = ws.root / ".git"
    git_dir.mkdir()
    (git_dir / "config").write_text("x")
    with pytest.raises(PolicyError):
        ws.resolve(".git/config")
    with pytest.raises(PolicyError):
        ws.resolve(".git/config", for_write=True)
    with pytest.raises(PolicyError):
        ws.resolve("sub/.git/hooks/pre-commit")


@pytest.mark.parametrize(
    "relpath",
    [
        ".github/workflows/ci.yml",
        ".claude/settings.json",
        ".codex/config.toml",
        ".mcp.json",
        ".workers/reviewer.md",
        "CLAUDE.md",
        "AGENTS.md",
    ],
)
def test_write_only_denials_are_readable_but_not_writable(ws: LocalWorkspace, relpath: str) -> None:
    full = ws.root / relpath
    full.parent.mkdir(parents=True, exist_ok=True)
    full.write_text("content")
    # Readable.
    assert ws.resolve(relpath, for_write=False) == full.resolve()
    # Not writable.
    with pytest.raises(PolicyError):
        ws.resolve(relpath, for_write=True)


def test_writing_through_existing_symlink_refused(ws: LocalWorkspace) -> None:
    target = ws.root / "real.txt"
    target.write_text("hi")
    link = ws.root / "link.txt"
    link.symlink_to(target)
    with pytest.raises(PolicyError):
        ws.resolve("link.txt", for_write=True)


def test_reading_through_existing_symlink_inside_root_allowed(ws: LocalWorkspace) -> None:
    target = ws.root / "real.txt"
    target.write_text("hi")
    link = ws.root / "link.txt"
    link.symlink_to(target)
    assert ws.resolve("link.txt", for_write=False) == target


def test_toctou_parent_replaced_by_symlink_mitigated_at_write_time(
    tmp_path: Path, ws: LocalWorkspace
) -> None:
    """resolve() is a point-in-time check; files.py re-validates immediately
    before the atomic replace (see workspace.py's TOCTOU docstring and
    tools/files.py's _atomic_write). Here we simulate the race directly: a
    parent directory that existed at resolve() time is swapped for a symlink
    to outside root before the write actually happens, and confirm the
    final target still is not writable by re-resolving (what files.py does).
    """
    subdir = ws.root / "subdir"
    subdir.mkdir()
    final = ws.resolve("subdir/file.txt", for_write=True)
    assert final == subdir / "file.txt"

    # Simulate the race: something swaps `subdir` for a symlink to outside root.
    outside_dir = tmp_path / "outside_dir"
    outside_dir.mkdir()
    os.rmdir(subdir)
    subdir.symlink_to(outside_dir)

    # A re-resolve immediately before writing (what _atomic_write's caller
    # should do for defense in depth) now correctly denies it.
    with pytest.raises(PolicyError):
        ws.resolve("subdir/file.txt", for_write=True)


def test_is_denied_helper_matches_resolve_semantics(ws: LocalWorkspace) -> None:
    (ws.root / "ok.txt").write_text("x")
    assert ws.is_denied("ok.txt") is False
    assert ws.is_denied(".env") is True
    assert ws.is_denied("../outside.txt") is True


# --------------------------------------------------------------------------- is_write_denied()


def test_is_write_denied_false_for_ordinary_path(ws: LocalWorkspace) -> None:
    (ws.root / "app.py").write_text("x")
    assert ws.is_write_denied("app.py") is False


def test_is_write_denied_true_for_write_denied_glob(ws: LocalWorkspace) -> None:
    full = ws.root / "CLAUDE.md"
    full.write_text("x")
    assert ws.is_write_denied("CLAUDE.md") is True


def test_is_write_denied_true_for_nested_write_denied_glob(ws: LocalWorkspace) -> None:
    full = ws.root / ".github" / "workflows" / "ci.yml"
    full.parent.mkdir(parents=True)
    full.write_text("x")
    assert ws.is_write_denied(".github/workflows/ci.yml") is True


def test_is_write_denied_true_for_secret_shaped_name(ws: LocalWorkspace) -> None:
    # Denied-read names (.env, keys, credential files) are also write-denied
    # -- is_write_denied runs the exact same resolve(for_write=True) path.
    assert ws.is_write_denied(".env") is True
    assert ws.is_write_denied("id_rsa") is True
    assert ws.is_write_denied("secrets.yaml") is True


def test_is_write_denied_true_for_git_internals(ws: LocalWorkspace) -> None:
    assert ws.is_write_denied(".git/config") is True


def test_is_write_denied_true_for_path_escaping_root(ws: LocalWorkspace) -> None:
    assert ws.is_write_denied("../outside.txt") is True


def test_is_write_denied_true_for_existing_symlink(ws: LocalWorkspace) -> None:
    target = ws.root / "real.txt"
    target.write_text("hi")
    link = ws.root / "link.txt"
    link.symlink_to(target)
    assert ws.is_write_denied("link.txt") is True


def test_is_write_denied_false_for_sensitive_but_allowed_path(ws: LocalWorkspace) -> None:
    # "Sensitive" paths are flagged, not denied -- is_write_denied must not
    # conflate the two tiers.
    (ws.root / "conftest.py").write_text("x")
    assert ws.is_write_denied("conftest.py") is False
    assert ws.is_sensitive("conftest.py") is True


def test_is_write_denied_never_raises_on_bad_input(ws: LocalWorkspace) -> None:
    assert ws.is_write_denied("a\x00.txt") is True
    assert ws.is_write_denied("") is True


# --------------------------------------------------------------------------- Write-policy table
#
# One parametrized table per policy tier: denied outright, allowed-but-flagged
# ("sensitive"), and ordinary source paths that must be neither.

_WRITE_DENIED_TABLE = [
    # Pre-existing deny entries.
    "CLAUDE.md",
    "AGENTS.md",
    ".claude/settings.json",
    ".codex/config.toml",
    ".mcp.json",
    ".workers/reviewer.md",
    ".github/workflows/ci.yml",
    # Newly added: CI/CD across providers.
    ".github/CODEOWNERS",
    ".gitlab-ci.yml",
    ".circleci/config.yml",
    ".travis.yml",
    "Jenkinsfile",
    "azure-pipelines.yml",
    "bitbucket-pipelines.yml",
    # Shell / direnv startup files.
    ".envrc",
    ".bashrc",
    ".zshrc",
    ".zprofile",
    ".profile",
    ".bash_profile",
    # Editor / IDE / devcontainer.
    ".vscode/settings.json",
    ".idea/workspace.xml",
    ".devcontainer/devcontainer.json",
    # Misc repo-level config that executes or governs execution later.
    ".pre-commit-config.yaml",
    ".gitattributes",
    ".gitmodules",
    ".husky/pre-commit",
    # Python import-time hooks.
    "sitecustomize.py",
    "usercustomize.py",
    "extra.pth",
    # Other assistants' agent-config equivalents.
    ".cursor/rules.json",
    ".cursorrules",
    ".windsurfrules",
    ".clinerules",
    ".aiderignore",
    ".roo/config.json",
    ".gemini/config.json",
    "GEMINI.md",
    "copilot-instructions.md",
    ".claude-plugin/plugin.json",
]

_SENSITIVE_TABLE = [
    "conftest.py",
    "tests/conftest.py",
    "Makefile",
    "makefile",
    "GNUmakefile",
    "justfile",
    "package.json",
    "pyproject.toml",
    "setup.py",
    "setup.cfg",
    "Dockerfile",
    "Dockerfile.prod",
    "docker-compose.yml",
    "docker-compose.prod.yaml",
    "tox.ini",
    "noxfile.py",
    "build.gradle",
    "build.gradle.kts",
    "pom.xml",
    "Cargo.toml",
    "build.rs",
    "go.mod",
    "scripts/deploy.sh",
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

_ORDINARY_ALLOWED_TABLE = [
    "src/app.py",
    "tests/test_x.py",
    "README.md",
    "docs/guide.md",
    "src/pkg/__init__.py",
    "notes.txt",
]


@pytest.mark.parametrize("relpath", _WRITE_DENIED_TABLE)
def test_write_policy_deny_table(ws: LocalWorkspace, relpath: str) -> None:
    full = ws.root / relpath
    full.parent.mkdir(parents=True, exist_ok=True)
    full.write_text("x")
    # Readable...
    assert ws.resolve(relpath, for_write=False) == full.resolve()
    # ...but never writable, and never merely "flagged" instead of denied.
    with pytest.raises(PolicyError):
        ws.resolve(relpath, for_write=True)
    assert ws.is_sensitive(relpath) is False


@pytest.mark.parametrize("relpath", _SENSITIVE_TABLE)
def test_write_policy_sensitive_table(ws: LocalWorkspace, relpath: str) -> None:
    full = ws.root / relpath
    full.parent.mkdir(parents=True, exist_ok=True)
    full.write_text("x")
    assert ws.resolve(relpath, for_write=True) == full.resolve()
    assert ws.is_sensitive(relpath) is True


@pytest.mark.parametrize("relpath", _ORDINARY_ALLOWED_TABLE)
def test_write_policy_ordinary_paths_allowed_and_unflagged(
    ws: LocalWorkspace, relpath: str
) -> None:
    full = ws.root / relpath
    full.parent.mkdir(parents=True, exist_ok=True)
    full.write_text("x")
    assert ws.resolve(relpath, for_write=True) == full.resolve()
    assert ws.is_sensitive(relpath) is False


# --------------------------------------------------------------------------- resolve() corpus


@pytest.mark.parametrize(
    "name",
    [
        ".env",
        ".ENV",
        ".Env",
        unicodedata.normalize("NFC", ".env"),
        unicodedata.normalize("NFD", ".env"),
        ".env.",  # trailing dot
        ".env ",  # trailing space
        ".env . ",  # trailing dot-space combo
    ],
)
def test_resolve_corpus_env_case_and_normalization_variants_denied(
    ws: LocalWorkspace, name: str
) -> None:
    with pytest.raises(PolicyError):
        ws.resolve(name)


@pytest.mark.parametrize(
    "bad",
    [
        "a\nb.txt",
        "a\tb.txt",
        "a\rb.txt",
        "a\x01b.txt",
        "a\x7fb.txt",  # DEL
        "dir/\nname.txt",
    ],
)
def test_resolve_corpus_control_characters_denied(ws: LocalWorkspace, bad: str) -> None:
    with pytest.raises(PolicyError):
        ws.resolve(bad)


def test_resolve_corpus_git_as_a_file_denied(ws: LocalWorkspace) -> None:
    # Git worktrees legitimately use a `.git` *file* (not directory)
    # containing a `gitdir:` pointer; the deny check is name-based, so it
    # must be refused either way.
    (ws.root / ".git").write_text("gitdir: ../real/.git\n")
    with pytest.raises(PolicyError):
        ws.resolve(".git")
    with pytest.raises(PolicyError):
        ws.resolve(".git", for_write=True)


def test_resolve_corpus_trailing_dot_space_on_other_denied_names(ws: LocalWorkspace) -> None:
    for variant in ("id_rsa.", "id_rsa ", "credentials.json.", "credentials.json "):
        with pytest.raises(PolicyError):
            ws.resolve(variant)


# --------------------------------------------------------------------------- is_sensitive()


def test_is_sensitive_false_for_path_outside_root(ws: LocalWorkspace, tmp_path: Path) -> None:
    outside = tmp_path / "package.json"
    assert ws.is_sensitive(outside) is False


def test_is_sensitive_accepts_absolute_and_relative_paths(ws: LocalWorkspace) -> None:
    (ws.root / "pyproject.toml").write_text("x")
    assert ws.is_sensitive("pyproject.toml") is True
    assert ws.is_sensitive(ws.root / "pyproject.toml") is True


def test_virtualenv_paths_are_write_denied(tmp_path: Path) -> None:
    ws = LocalWorkspace(tmp_path)
    for denied in (".venv/bin/python", "sub/.venv/bin/activate", "venv/pyvenv.cfg"):
        assert ws.is_write_denied(denied), denied
    for allowed in ("src/venv_utils.py", "docs/venv.md"):
        assert not ws.is_write_denied(allowed), allowed
