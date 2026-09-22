"""Regression tests for the secreview-0110-fix findings 1, 2, 4 (real Seatbelt).

Findings recap (see sandbox.py's `_secret_dir_denies`/`_secret_name_denies` and
tests/test_secfix_secret_names.py for the name-table parity corpus):

1. HIGH: `_secret_dir_denies` spliced a Python-only `(?:...)` non-capturing group into
   the Seatbelt profile. Seatbelt's regex engine is POSIX ERE, which has no `(?:...)`
   syntax, so the first alternative in the group was silently swallowed and
   `<ws>/secrets/**` stayed readable in sandboxed bash.
2. HIGH: the secret-name/dir deny rules only covered `file-read-data
   file-map-executable`, so a sandboxed `mv .env notes.md` (or `secrets.yaml`->`x.md`,
   `secrets/`->`pkg`) succeeded, and the Read tool (outside the sandbox) then served the
   plaintext at the new name.
4. MEDIUM: the credential-family regex's inner wildcard spanned `/` in Seatbelt,
   over-denying `credential_provider/README.txt` and similar subtrees.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from anymodel_subagents.tools import sandbox
from anymodel_subagents.tools.workspace import LocalWorkspace

requires_seatbelt = pytest.mark.skipif(
    sandbox.detect() != "seatbelt",
    reason="requires a working Seatbelt (sandbox-exec) on this machine",
)


def _run(argv: list[str], *, cwd: Path, timeout: float = 20) -> subprocess.CompletedProcess:
    return subprocess.run(
        argv, capture_output=True, text=True, timeout=timeout, cwd=str(cwd), check=False
    )


@pytest.fixture
def dirs(tmp_path: Path) -> tuple[Path, Path]:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    tmp = tmp_path / "tmp"
    tmp.mkdir()
    return workspace, tmp


# --------------------------------------------------------------------------- finding 1


def test_no_seatbelt_regex_uses_python_only_non_capturing_groups(tmp_path: Path) -> None:
    """No regex spliced into the Seatbelt profile may contain `(?:` (or any `(?`):
    Seatbelt's regex engine is POSIX ERE, which has no non-capturing-group syntax, and a
    `?` right after `(` silently breaks the group instead of raising a compile error."""
    argv = sandbox.build_seatbelt_argv(
        ["/bin/true"], workspace=tmp_path / "ws", tmp=tmp_path / "tmp", deny_read=[]
    )
    profile = argv[argv.index("-p") + 1]
    assert "(?" not in profile, profile


@requires_seatbelt
def test_seatbelt_denies_reading_inside_a_secrets_directory(dirs: tuple[Path, Path]) -> None:
    """`<ws>/secrets/**` must be denied: this is the exact bug from finding 1 (the
    `(?:...)` group swallowed the first alternative, "secrets", so this specific case
    stayed readable even though `.secrets`/`credentials`/`.credentials` did not)."""
    workspace, tmp = dirs
    (workspace / "secrets").mkdir()
    (workspace / "secrets" / "prod.json").write_text('{"key": "PROD-SECRET-DECOY"}')

    argv = sandbox.wrap(
        ["/bin/cat", str(workspace / "secrets" / "prod.json")],
        workspace=workspace,
        tmp=tmp,
        deny_read=[],
        kind="seatbelt",
    )
    proc = _run(argv, cwd=workspace)
    assert proc.returncode != 0, proc.stdout
    assert "PROD-SECRET-DECOY" not in proc.stdout


# --------------------------------------------------------------------------- finding 2


@requires_seatbelt
def test_seatbelt_denies_renaming_secret_shaped_files_and_dirs(dirs: tuple[Path, Path]) -> None:
    """A sandboxed `mv` must not be able to rename `.env`/`secrets.yaml`/`secrets/` to an
    allowed name: rename only needs write access to unlink/recreate the path, not read
    access, so the read-only denies alone don't stop it."""
    workspace, tmp = dirs
    (workspace / ".env").write_text("AWS_SECRET_ACCESS_KEY=DECOY-NOT-REAL")
    (workspace / "secrets.yaml").write_text("api_key: DECOY-NOT-REAL")
    (workspace / "secrets").mkdir()
    (workspace / "secrets" / "db.yml").write_text("password: DECOY-NOT-REAL")

    cases = [
        (".env", "notes.md"),
        ("secrets.yaml", "s.md"),
        ("secrets", "pkg"),
    ]
    for src, dst in cases:
        argv = sandbox.wrap(
            ["/bin/mv", str(workspace / src), str(workspace / dst)],
            workspace=workspace,
            tmp=tmp,
            deny_read=[],
            kind="seatbelt",
        )
        proc = _run(argv, cwd=workspace)
        assert proc.returncode != 0, f"{src} -> {dst} should have been denied: {proc.stdout}"
        assert (workspace / src).exists(), f"{src} must still exist after the denied rename"
        assert not (workspace / dst).exists(), f"{dst} must not have been created"


@requires_seatbelt
def test_seatbelt_still_allows_ordinary_writes_next_to_a_dotenv(dirs: tuple[Path, Path]) -> None:
    """The write-side secret denies must not regress ordinary worker writes: creating a
    normal file in the same directory as a `.env` (or editing an unrelated file) must
    keep working."""
    workspace, tmp = dirs
    (workspace / ".env").write_text("AWS_SECRET_ACCESS_KEY=DECOY-NOT-REAL")

    argv = sandbox.wrap(
        ["/bin/sh", "-c", "echo hello > notes.md && echo more >> notes.md"],
        workspace=workspace,
        tmp=tmp,
        deny_read=[],
        kind="seatbelt",
    )
    proc = _run(argv, cwd=workspace)
    assert proc.returncode == 0, proc.stderr
    assert (workspace / "notes.md").read_text() == "hello\nmore\n"


@requires_seatbelt
def test_seatbelt_allows_env_example_write(dirs: tuple[Path, Path]) -> None:
    """The write-side deny must still carve out the `.env.example`-style exceptions,
    same as the read side."""
    workspace, tmp = dirs

    argv = sandbox.wrap(
        ["/bin/sh", "-c", "echo TEMPLATE=1 > .env.example"],
        workspace=workspace,
        tmp=tmp,
        deny_read=[],
        kind="seatbelt",
    )
    proc = _run(argv, cwd=workspace)
    assert proc.returncode == 0, proc.stderr
    assert (workspace / ".env.example").read_text() == "TEMPLATE=1\n"


def test_secret_name_denies_emits_write_ops_lines(tmp_path: Path) -> None:
    """Unit test for the argv construction: `_secret_name_denies`/`_secret_dir_denies`
    called with `_WRITE_OPS` must actually be wired into `build_seatbelt_argv`, not just
    exist as dead code."""
    argv = sandbox.build_seatbelt_argv(
        ["/bin/true"], workspace=tmp_path / "ws", tmp=tmp_path / "tmp", deny_read=[]
    )
    profile = argv[argv.index("-p") + 1]
    assert "(deny file-write* (regex" in profile


# --------------------------------------------------------------------------- finding 4


@requires_seatbelt
def test_seatbelt_reads_ordinary_files_under_lookalike_credential_directories(
    dirs: tuple[Path, Path],
) -> None:
    """`credential_provider/README.txt` must stay readable (an ordinary package
    directory whose name merely contains "credential"), while a genuinely
    credential-shaped file nested in the same directory, or under a truly
    secret-shaped directory, stays denied."""
    workspace, tmp = dirs
    (workspace / "credential_provider").mkdir()
    (workspace / "credential_provider" / "README.txt").write_text("ORDINARY-PACKAGE-DOC")
    (workspace / "credential_provider" / "credentials.json").write_text(
        '{"key": "PROVIDER-SECRET-DECOY"}'
    )
    (workspace / "aws_credentials").mkdir()
    (workspace / "aws_credentials" / "service-account.json").write_text(
        '{"key": "AWS-SECRET-DECOY"}'
    )

    def cat(rel: str) -> subprocess.CompletedProcess:
        argv = sandbox.wrap(
            ["/bin/cat", str(workspace / rel)],
            workspace=workspace,
            tmp=tmp,
            deny_read=[],
            kind="seatbelt",
        )
        return _run(argv, cwd=workspace)

    ok = cat("credential_provider/README.txt")
    assert ok.returncode == 0, ok.stderr
    assert "ORDINARY-PACKAGE-DOC" in ok.stdout

    denied_nested = cat("credential_provider/credentials.json")
    assert denied_nested.returncode != 0
    assert "PROVIDER-SECRET-DECOY" not in denied_nested.stdout

    denied_dir = cat("aws_credentials/service-account.json")
    assert denied_dir.returncode != 0
    assert "AWS-SECRET-DECOY" not in denied_dir.stdout


# --------------------------------------------------------------------- secreview-0110-fix2


@requires_seatbelt
def test_seatbelt_profile_compiles_for_workspace_path_with_space_and_unusual_char(
    tmp_path: Path,
) -> None:
    """The workspace path is spliced into the profile only via `-D` params (never
    string-interpolated), so a path containing a space or shell-special characters must
    not corrupt the compiled profile -- a malformed profile makes sandbox-exec exit 65
    and run nothing. Regression guard for finding 2's new `[. \\t]*` bracket addition:
    it must not break profile compilation either."""
    workspace = tmp_path / "odd $dir & (weird) name"
    workspace.mkdir()
    tmp = tmp_path / "tmp"
    tmp.mkdir()
    (workspace / "ok.txt").write_text("hello\n")

    argv = sandbox.wrap(
        ["/bin/cat", str(workspace / "ok.txt")],
        workspace=workspace,
        tmp=tmp,
        deny_read=[],
        kind="seatbelt",
    )
    proc = _run(argv, cwd=workspace)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == "hello\n"


@requires_seatbelt
@pytest.mark.parametrize(
    "case",
    [
        pytest.param("hardlink", id="hardlink-ln"),
        pytest.param("clonefile", id="cp-c-clonefile"),
    ],
)
def test_seatbelt_denies_extraction_via_hardlink_and_clonefile(
    dirs: tuple[Path, Path], case: str
) -> None:
    """Beyond plain rename (finding 2 of the 0110 fix, covered above): a hardlink or a
    clonefile-backed `cp -c` must not be able to surface `.env`'s plaintext under an
    allowed name either -- both only need directory-write access to create the new
    entry, not read access to the source's data, the same gap `mv` had."""
    workspace, tmp = dirs
    (workspace / ".env").write_text("AWS_SECRET_ACCESS_KEY=WD-DECOY-NOT-REAL")

    cmd = (
        "ln .env x.md && cat x.md" if case == "hardlink" else "cp -c .env x.md 2>&1; cat x.md 2>&1"
    )
    argv = sandbox.wrap(
        ["/bin/sh", "-c", cmd], workspace=workspace, tmp=tmp, deny_read=[], kind="seatbelt"
    )
    proc = _run(argv, cwd=workspace)
    assert "WD-DECOY-NOT-REAL" not in (proc.stdout + proc.stderr)
    assert not (workspace / "x.md").exists()


@requires_seatbelt
def test_seatbelt_parent_dir_rename_denied_and_read_tool_still_denies(
    dirs: tuple[Path, Path],
) -> None:
    """Renaming the (ordinary, non-secret-shaped) parent directory `config` is not
    itself something the secret-name write-denies touch -- only the SECRET-shaped
    entry's own name is protected, so `mv config cfg` is allowed as an ordinary write.
    What must still hold: the nested `.env` stays unreadable under its new path (the
    deny pattern matches the final component by suffix, independent of any ancestor
    name), and the file tools (`LocalWorkspace`, which runs outside the sandbox) deny
    the secret-shaped final component under EITHER possible ancestor name -- so a
    worker gains nothing by renaming the parent either in or out of the sandbox."""
    workspace, tmp = dirs
    (workspace / "config").mkdir()
    (workspace / "config" / ".env").write_text("NESTED=WD-DECOY-NOT-REAL")

    argv = sandbox.wrap(
        ["/bin/sh", "-c", "mv config cfg; cat cfg/.env 2>&1; cat config/.env 2>&1"],
        workspace=workspace,
        tmp=tmp,
        deny_read=[],
        kind="seatbelt",
    )
    proc = _run(argv, cwd=workspace)
    assert "WD-DECOY-NOT-REAL" not in proc.stdout

    ws = LocalWorkspace(root=workspace)
    assert ws.is_denied("cfg/.env") is True
    assert ws.is_denied("config/.env") is True
