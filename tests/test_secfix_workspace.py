"""Security-fix regression tests for the LocalWorkspace policy tables.

Three fixes, one section each:

1. Read-deny (`is_denied`) gaps: ordinary secret spellings that were readable
   (`prod.env`, `credentials.yaml`, `.git-credentials`, `id_dsa`, ...), plus
   the invariant that the `.env.example`-style carve-out is per-component --
   a directory named `.env.example` must not un-deny anything beneath it.
2. Write-deny additions: files that execute on the user's machine later
   (shell/vim startup files, git hooks, gradle wrapper, more CI providers).
3. Sensitive-flag additions: build/run entrypoints a worker may legitimately
   edit but that execute outside the current review.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from anymodel_subagents.tools.workspace import LocalWorkspace
from anymodel_subagents.types import PolicyError


@pytest.fixture
def ws(tmp_path: Path) -> LocalWorkspace:
    root = tmp_path / "repo"
    root.mkdir()
    return LocalWorkspace(root=root)


# --------------------------------------------------------------------------- 1. read-deny


@pytest.mark.parametrize(
    "name",
    [
        "prod.env",
        "PROD.ENV",  # names are casefolded before matching
        "app.env",  # family: any '<name>.env', not just prod
        "foo.env.bak",
        "FOO.ENV.BAK",
        "config/prod.env",  # denied at any path depth
        "credentials.yaml",
        "CREDENTIALS.YAML",
        "credentials.yml",
        "credentials.ini",
        "credentials.toml",
        "credentials.csv",
        "credentials.txt",
        "credentials",  # no extension
        "credentials.backup.yaml",  # real extension is still yaml
        "credentials.json",  # already denied; now via the extension family
        ".git-credentials",
        "authorized_keys",
        "keys/authorized_keys",
        "id_dsa",
        "ID_DSA",
        "id_dsa.pub",  # matches the established id_rsa.pub behaviour
        "server_rsa",  # family: '*_rsa' private-key names
        "github_rsa",
        "app.jks",
        "APP.JKS",
        "client.ovpn",
        "secret_key_base",
        "SECRET_KEY_BASE",
        ".netrc",  # already covered before the fix; pinned here
        "sub/.netrc",
    ],
)
def test_read_denied_secret_spellings(ws: LocalWorkspace, name: str) -> None:
    assert ws.is_denied(name) is True, name
    assert ws.is_denied(f"sub/dir/{name}") is True, f"sub/dir/{name}"
    # Secret-shaped names are also refused for writes via the same resolve().
    assert ws.is_write_denied(name) is True, name


@pytest.mark.parametrize(
    "name",
    [
        ".env.example",
        ".env.sample",
        ".env.template",
        "environment.py",
        "env.local",  # too generic to deny -- deliberately left readable
        "docs/credentials.md",  # credentials.<ext> only for machine-config extensions
        "credentials.md",
        "credentials.py",
    ],
)
def test_read_allowed_lookalikes(ws: LocalWorkspace, name: str) -> None:
    full = ws.root / name
    full.parent.mkdir(parents=True, exist_ok=True)
    full.write_text("harmless")
    assert ws.is_denied(name) is False, name
    assert ws.resolve(name) == full.resolve()


def test_env_example_carveout_is_per_component_not_prefix(ws: LocalWorkspace) -> None:
    """A directory named `.env.example` is readable, but it must not un-deny
    anything beneath it: the exception applies to that component only."""
    d = ws.root / ".env.example"
    d.mkdir()
    (d / "README.md").write_text("sample env vars")
    (d / ".env").write_text("SECRET=1")
    (d / "id_rsa").write_text("key")
    assert ws.resolve(".env.example") == d
    assert ws.resolve(".env.example/README.md") == d / "README.md"
    assert ws.is_denied(".env.example/.env") is True
    assert ws.is_denied(".env.example/id_rsa") is True
    assert ws.is_denied(".env.example/credentials.yaml") is True


# --------------------------------------------------------------------------- 2. write-deny


@pytest.mark.parametrize(
    "relpath",
    [
        # Shell startup files not already covered (.bashrc/.zshrc/.profile/...).
        ".zshenv",
        ".ZSHENV",
        ".bash_logout",
        ".kshrc",
        # Editor config that executes code (modelines, plugins, autocommands).
        ".vimrc",
        ".Vimrc",
        # Git hooks usable via core.hooksPath.
        ".githooks/pre-commit",
        # Build-tool entrypoints that fetch and run code.
        "gradlew",
        "GRADLEW",
        "gradlew.bat",
        "gradle/wrapper/gradle-wrapper.properties",
        # CI/CD providers missing from the original list.
        ".gitlab/ci/child-pipeline.yml",
        ".buildkite/pipeline.yml",
        ".drone.yml",
        "appveyor.yml",
        ".appveyor.yml",
        ".woodpecker.yml",
        ".woodpecker/pipeline.yml",
        "cloudbuild.yaml",
        ".gitpod.yml",
        "codemagic.yaml",
        ".teamcity/settings.kts",
    ],
)
def test_write_denied_exec_later_paths(ws: LocalWorkspace, relpath: str) -> None:
    full = ws.root / relpath
    full.parent.mkdir(parents=True, exist_ok=True)
    full.write_text("x")
    # Readable...
    assert ws.resolve(relpath, for_write=False) == full.resolve()
    # ...but never writable.
    assert ws.is_write_denied(relpath) is True
    with pytest.raises(PolicyError):
        ws.resolve(relpath, for_write=True)


def test_write_deny_trumps_sensitive_overlap(ws: LocalWorkspace) -> None:
    """`gradlew.bat` is deny-tier even though the `*.bat` sensitive family also
    matches it. `is_sensitive()` is a pure post-resolve name classifier that
    does not consult the deny list, so the deny tier is what governs writes."""
    (ws.root / "gradlew.bat").write_text("x")
    assert ws.is_write_denied("gradlew.bat") is True
    with pytest.raises(PolicyError):
        ws.resolve("gradlew.bat", for_write=True)
    assert ws.is_sensitive("gradlew.bat") is True


# --------------------------------------------------------------------------- 3. sensitive


@pytest.mark.parametrize(
    "relpath",
    [
        # Shell-script families beyond the existing *.sh.
        "scripts/bootstrap.bash",
        "scripts/run.zsh",
        "scripts/run.ksh",
        "scripts/run.fish",
        "scripts/deploy.ps1",
        "scripts/setup.bat",
        "scripts/setup.cmd",
        "scripts/deploy.SH",  # casefolded like everything else
        # JS/TS build-config families (webpack/vite/rollup/jest/next/...).
        "webpack.config.js",
        "vite.config.ts",
        "rollup.config.mjs",
        "jest.config.cjs",
        "next.config.js",
        "sub/nuxt.config.ts",
        # Other build entrypoints that run outside the current review.
        "binding.gyp",
        "Vagrantfile",
        "meson.build",
        "sub/meson.build",
        "SConstruct",
        "build.xml",
        "requirements.txt",
        "requirements-dev.txt",
        "requirements_dev.txt",
    ],
)
def test_sensitive_exec_later_paths(ws: LocalWorkspace, relpath: str) -> None:
    full = ws.root / relpath
    full.parent.mkdir(parents=True, exist_ok=True)
    full.write_text("x")
    # Writable, but flagged for review -- never denied, never unflagged.
    assert ws.resolve(relpath, for_write=True) == full.resolve()
    assert ws.is_write_denied(relpath) is False
    assert ws.is_sensitive(relpath) is True
