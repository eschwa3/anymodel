"""Tests for tools/sandbox.py: detection, argv construction, and (where the
machine actually has Seatbelt) real containment behavior.

The bwrap argv-construction tests run unconditionally (they only assert on
the list of strings `build_bwrap_argv` returns, never invoking `bwrap`
itself) since bwrap cannot be installed/exercised on this macOS dev machine.
The Seatbelt tests are split into argv-construction (unconditional) and real
escape tests (skipped unless `sandbox.detect() == "seatbelt"`).
"""

from __future__ import annotations

import itertools
import socket
import subprocess
import sys
from pathlib import Path

import pytest

from anymodel_subagents.tools import sandbox
from anymodel_subagents.types import PolicyError

pytestmark = pytest.mark.skipif(sys.platform != "darwin", reason="written against macOS/Seatbelt")

# The real interpreter, not /usr/bin/python3: that one is an Apple shim that resolves the
# toolchain through xcrun on every call, which takes seconds inside the sandbox (tens of seconds
# on a loaded machine) and made these tests flaky.
_PYTHON = (
    str(sandbox.real_toolchain_bin() / "python3")
    if sandbox.real_toolchain_bin()
    else sys.executable
)


# --------------------------------------------------------------------------- detect()


def test_detect_returns_seatbelt_on_this_machine() -> None:
    # This dev machine is macOS with a working /usr/bin/sandbox-exec; if this
    # ever fails, every other "real seatbelt" test below will also skip.
    assert sandbox.detect() == "seatbelt"


def test_detect_returns_none_when_seatbelt_binary_missing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(sandbox, "_SEATBELT_EXE", str(tmp_path / "no-such-sandbox-exec"))
    assert sandbox.detect() is None


def test_detect_returns_none_when_probe_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run(*args, **kwargs):
        return subprocess.CompletedProcess(args, returncode=1)

    monkeypatch.setattr(sandbox.subprocess, "run", fake_run)
    assert sandbox.detect() is None


def test_detect_returns_none_when_probe_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run(*args, **kwargs):
        raise OSError("nope")

    monkeypatch.setattr(sandbox.subprocess, "run", fake_run)
    assert sandbox.detect() is None


def test_detect_linux_checks_bwrap_on_path(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sandbox.sys, "platform", "linux")
    monkeypatch.setattr(sandbox.shutil, "which", lambda name: None)
    assert sandbox.detect() is None


def test_detect_linux_probes_bwrap_when_present(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sandbox.sys, "platform", "linux")
    monkeypatch.setattr(sandbox.shutil, "which", lambda name: "/usr/bin/bwrap")

    def fake_run(*args, **kwargs):
        return subprocess.CompletedProcess(args, returncode=0)

    monkeypatch.setattr(sandbox.subprocess, "run", fake_run)
    assert sandbox.detect() == "bwrap"


# --------------------------------------------------------------------------- default_deny_read


def test_default_deny_read_includes_state_dir_and_credential_dirs(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    denied = sandbox.default_deny_read(state_dir)
    assert Path(state_dir) in denied
    home = Path.home()
    assert (home / ".ssh") in denied
    assert (home / ".aws") in denied
    assert (home / ".claude") in denied
    assert (home / ".codex") in denied
    assert (home / "Library" / "Keychains") in denied


# --------------------------------------------------------------------------- build_seatbelt_argv


def test_build_seatbelt_argv_passes_paths_as_params_not_interpolated(tmp_path: Path) -> None:
    workspace = tmp_path / "weird dir 'quote"
    workspace.mkdir()
    tmp = tmp_path / "tmp"
    tmp.mkdir()
    argv = sandbox.build_seatbelt_argv(
        ["/bin/echo", "hi"], workspace=workspace, tmp=tmp, deny_read=[]
    )
    assert argv[0] == "/usr/bin/sandbox-exec"
    # The path itself must appear only as a -D parameter value, never spliced
    # into the -p profile text.
    profile_index = argv.index("-p") + 1
    profile = argv[profile_index]
    assert str(workspace.resolve()) not in profile
    assert 'subpath (param "WORKSPACE")' in profile
    # And the param must carry the real (resolved) path.
    params = {}
    i = 0
    while i < len(argv):
        if argv[i] == "-D":
            name, _, value = argv[i + 1].partition("=")
            params[name] = value
            i += 2
        else:
            i += 1
    assert params["WORKSPACE"] == str(workspace.resolve())
    assert params["TMP"] == str(tmp.resolve())
    assert params["WORKSPACE_GIT"] == str(workspace.resolve() / ".git")
    assert argv[-2:] == ["/bin/echo", "hi"]


def test_build_seatbelt_argv_denies_network_and_git_writes(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    tmp = tmp_path / "tmp"
    tmp.mkdir()
    argv = sandbox.build_seatbelt_argv(["/bin/true"], workspace=workspace, tmp=tmp, deny_read=[])
    profile = argv[argv.index("-p") + 1]
    assert "(deny network*)" in profile
    assert "WORKSPACE_GIT" in profile
    assert "(allow process-fork)" in profile
    assert "(allow process-exec)" in profile


def test_build_seatbelt_argv_adds_one_deny_rule_per_deny_read_path(tmp_path: Path) -> None:
    deny_read = [tmp_path / "a", tmp_path / "b", tmp_path / "c"]
    argv = sandbox.build_seatbelt_argv(
        ["/bin/true"], workspace=tmp_path / "ws", tmp=tmp_path / "tmp", deny_read=deny_read
    )
    profile = argv[argv.index("-p") + 1]
    assert profile.count("(deny file-read*") == len(deny_read)


def test_build_seatbelt_argv_is_deny_by_default_with_no_mach_lookup_or_network(
    tmp_path: Path,
) -> None:
    # The profile-string assertions the adversarial review asked for: the
    # base policy is deny-by-default (no blanket `(allow default)`), and
    # mach-lookup/network are denied outright with no exceptions layered
    # back in -- this is what closes the keychain/pasteboard/Apple
    # Events/LaunchServices findings (see module docstring and
    # test_bash.py's real-exploit tests for the executed proof).
    argv = sandbox.build_seatbelt_argv(
        ["/bin/true"], workspace=tmp_path / "ws", tmp=tmp_path / "tmp", deny_read=[]
    )
    profile = argv[argv.index("-p") + 1]
    assert "(deny default)" in profile
    assert "(allow default)" not in profile
    assert "(deny mach-lookup)" in profile
    assert "(deny network*)" in profile
    # No mach service is ever explicitly allowed back in.
    assert "(allow mach-lookup" not in profile
    assert "mach-lookup" not in profile.replace("(deny mach-lookup)", "")


def test_build_seatbelt_argv_extra_read_adds_subpath_params(tmp_path: Path) -> None:
    extra_a = tmp_path / "repo" / ".git" / "worktrees" / "job1"
    extra_a.mkdir(parents=True)
    extra_b = tmp_path / "repo" / ".git"
    argv = sandbox.build_seatbelt_argv(
        ["/bin/true"],
        workspace=tmp_path / "ws",
        tmp=tmp_path / "tmp",
        deny_read=[],
        extra_read=(extra_a, extra_b),
    )
    profile = argv[argv.index("-p") + 1]
    params = {}
    i = 0
    while i < len(argv):
        if argv[i] == "-D":
            name, _, value = argv[i + 1].partition("=")
            params[name] = value
            i += 2
        else:
            i += 1
    assert params["EXTRA_READ_0"] == str(extra_a.resolve())
    assert params["EXTRA_READ_1"] == str(extra_b.resolve())
    assert 'subpath (param "EXTRA_READ_0")' in profile
    assert 'subpath (param "EXTRA_READ_1")' in profile


# --------------------------------------------------------------------------- build_bwrap_argv (construction only)


def test_build_bwrap_argv_binds_workspace_and_tmp(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    tmp = tmp_path / "tmp"
    tmp.mkdir()
    argv = sandbox.build_bwrap_argv(["pytest", "-q"], workspace=workspace, tmp=tmp, deny_read=[])
    assert "--bind" in argv
    ws_str = str(workspace.resolve())
    tmp_str = str(tmp.resolve())
    bind_indices = [i for i, tok in enumerate(argv) if tok == "--bind"]
    assert any(argv[i + 1] == ws_str and argv[i + 2] == ws_str for i in bind_indices)
    # tmp is bound onto /tmp inside the sandbox
    assert any(argv[i + 1] == tmp_str and argv[i + 2] == "/tmp" for i in bind_indices)
    assert argv[-2:] == ["pytest", "-q"]
    assert argv[-3] == "--"


def test_build_bwrap_argv_unshares_all_and_drops_caps(tmp_path: Path) -> None:
    argv = sandbox.build_bwrap_argv(
        ["true"], workspace=tmp_path / "ws", tmp=tmp_path / "tmp", deny_read=[]
    )
    # --unshare-all covers net/pid/ipc/uts/user isolation in one flag,
    # replacing the previous individual --unshare-net/--unshare-pid pair.
    assert "--unshare-all" in argv
    assert "--unshare-net" not in argv
    assert argv[argv.index("--cap-drop") + 1] == "ALL"
    assert "--die-with-parent" in argv
    assert "--new-session" in argv


def test_build_bwrap_argv_ro_binds_existing_git_dir(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    (workspace / ".git").mkdir(parents=True)
    tmp = tmp_path / "tmp"
    tmp.mkdir()
    argv = sandbox.build_bwrap_argv(["true"], workspace=workspace, tmp=tmp, deny_read=[])
    git_str = str((workspace / ".git").resolve())
    ro_bind_indices = [i for i, tok in enumerate(argv) if tok == "--ro-bind"]
    assert any(argv[i + 1] == git_str and argv[i + 2] == git_str for i in ro_bind_indices)


def test_build_bwrap_argv_skips_git_ro_bind_when_absent(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    tmp = tmp_path / "tmp"
    tmp.mkdir()
    argv = sandbox.build_bwrap_argv(["true"], workspace=workspace, tmp=tmp, deny_read=[])
    # No blanket `--ro-bind / /` -- only the specific system/toolchain dirs
    # that exist on this machine, none of which is the (absent) workspace
    # git dir.
    assert not any(a == "/" and b == "/" for a, b in itertools.pairwise(argv))
    git_str = str((workspace / ".git").resolve())
    ro_bind_pairs = {(argv[i + 1], argv[i + 2]) for i, tok in enumerate(argv) if tok == "--ro-bind"}
    assert (git_str, git_str) not in ro_bind_pairs


def test_build_bwrap_argv_ro_binds_existing_system_dirs(tmp_path: Path) -> None:
    argv = sandbox.build_bwrap_argv(
        ["true"], workspace=tmp_path / "ws", tmp=tmp_path / "tmp", deny_read=[]
    )
    ro_bind_pairs = {(argv[i + 1], argv[i + 2]) for i, tok in enumerate(argv) if tok == "--ro-bind"}
    # /usr and /bin exist on every machine this runs on (macOS dev box today,
    # a real Linux target eventually); real system dirs get bound, absent
    # ones (checked elsewhere) don't.
    assert ("/usr", "/usr") in ro_bind_pairs
    assert ("/bin", "/bin") in ro_bind_pairs


def test_build_bwrap_argv_tmpfs_shadows_only_existing_deny_paths(tmp_path: Path) -> None:
    existing = tmp_path / "secret"
    existing.mkdir()
    missing = tmp_path / "does-not-exist"
    argv = sandbox.build_bwrap_argv(
        ["true"],
        workspace=tmp_path / "ws",
        tmp=tmp_path / "tmp",
        deny_read=[existing, missing],
    )
    tmpfs_values = [argv[i + 1] for i, tok in enumerate(argv) if tok == "--tmpfs"]
    assert str(existing.resolve()) in tmpfs_values
    assert str(missing) not in tmpfs_values
    assert len(tmpfs_values) == 1


# --------------------------------------------------------------------------- wrap()


def test_wrap_dispatches_to_seatbelt(tmp_path: Path) -> None:
    argv = sandbox.wrap(
        ["/bin/true"],
        workspace=tmp_path / "ws",
        tmp=tmp_path / "tmp",
        deny_read=[],
        kind="seatbelt",
    )
    assert argv[0] == "/usr/bin/sandbox-exec"


def test_wrap_dispatches_to_bwrap(tmp_path: Path) -> None:
    argv = sandbox.wrap(
        ["true"], workspace=tmp_path / "ws", tmp=tmp_path / "tmp", deny_read=[], kind="bwrap"
    )
    assert "--unshare-all" in argv


def test_wrap_raises_policy_error_when_no_sandbox(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sandbox, "detect", lambda: None)
    with pytest.raises(PolicyError):
        sandbox.wrap(["true"], workspace=tmp_path / "ws", tmp=tmp_path / "tmp", deny_read=[])


def test_wrap_auto_detects_when_kind_not_given(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sandbox, "detect", lambda: "seatbelt")
    argv = sandbox.wrap(
        ["/bin/true"], workspace=tmp_path / "ws", tmp=tmp_path / "tmp", deny_read=[]
    )
    assert argv[0] == "/usr/bin/sandbox-exec"


# --------------------------------------------------------------------------- Real Seatbelt escape tests


requires_seatbelt = pytest.mark.skipif(
    sandbox.detect() != "seatbelt",
    reason="requires a working Seatbelt (sandbox-exec) on this machine",
)


def _run_sandboxed_python(
    code: str, *, workspace: Path, tmp: Path, deny_read: list[Path], timeout: float = 10
) -> subprocess.CompletedProcess:
    argv = sandbox.wrap(
        [_PYTHON, "-c", code],
        workspace=workspace,
        tmp=tmp,
        deny_read=deny_read,
        kind="seatbelt",
    )
    # cwd must be the workspace, same as the real Bash tool always sets it:
    # with `-c`, Python puts '' (cwd) on sys.path[0], and a cwd outside the
    # allowlist (e.g. the test runner's own working directory) makes even
    # import machinery's directory-listing fail closed under the
    # deny-by-default profile.
    return subprocess.run(
        argv, capture_output=True, text=True, timeout=timeout, check=False, cwd=str(workspace)
    )


@pytest.fixture
def sandboxed_dirs(tmp_path: Path) -> tuple[Path, Path]:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    tmp = tmp_path / "tmp"
    tmp.mkdir()
    return workspace, tmp


@requires_seatbelt
def test_seatbelt_denies_write_outside_workspace(tmp_path: Path, sandboxed_dirs) -> None:
    workspace, tmp = sandboxed_dirs
    outside = tmp_path / "outside.txt"
    code = f"open({str(outside)!r}, 'w').write('pwned')"
    _run_sandboxed_python(code, workspace=workspace, tmp=tmp, deny_read=[])
    assert not outside.exists()


@requires_seatbelt
def test_seatbelt_denies_write_to_git_hooks(sandboxed_dirs) -> None:
    workspace, tmp = sandboxed_dirs
    (workspace / ".git" / "hooks").mkdir(parents=True)
    hook = workspace / ".git" / "hooks" / "pre-commit"
    code = f"open({str(hook)!r}, 'w').write('evil')"
    _run_sandboxed_python(code, workspace=workspace, tmp=tmp, deny_read=[])
    assert not hook.exists()


@requires_seatbelt
def test_seatbelt_denies_read_of_deny_read_path(sandboxed_dirs) -> None:
    workspace, tmp = sandboxed_dirs
    secret_dir = tmp.parent / "secretdir"
    secret_dir.mkdir()
    secret = secret_dir / "id_rsa"
    secret.write_text("SUPER-SECRET")
    code = f"print(open({str(secret)!r}).read())"
    result = _run_sandboxed_python(code, workspace=workspace, tmp=tmp, deny_read=[secret_dir])
    assert "SUPER-SECRET" not in result.stdout


@requires_seatbelt
def test_seatbelt_denies_inbound_localhost_connection(sandboxed_dirs) -> None:
    workspace, tmp = sandboxed_dirs
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = listener.getsockname()[1]
    try:
        code = (
            "import socket\n"
            "s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)\n"
            "s.settimeout(2)\n"
            "try:\n"
            f"    s.connect(('127.0.0.1', {port}))\n"
            "    print('CONNECTED')\n"
            "except OSError as e:\n"
            "    print('BLOCKED', e)\n"
        )
        result = _run_sandboxed_python(code, workspace=workspace, tmp=tmp, deny_read=[], timeout=8)
        assert "CONNECTED" not in result.stdout
        assert "BLOCKED" in result.stdout
    finally:
        listener.close()


@requires_seatbelt
def test_seatbelt_denies_outbound_connection(sandboxed_dirs) -> None:
    workspace, tmp = sandboxed_dirs
    code = (
        "import socket\n"
        "s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)\n"
        "s.settimeout(2)\n"
        "try:\n"
        "    s.connect(('93.184.216.34', 80))\n"
        "    print('CONNECTED')\n"
        "except OSError as e:\n"
        "    print('BLOCKED', e)\n"
    )
    result = _run_sandboxed_python(code, workspace=workspace, tmp=tmp, deny_read=[], timeout=8)
    assert "CONNECTED" not in result.stdout
    assert "BLOCKED" in result.stdout


@requires_seatbelt
def test_seatbelt_allows_write_inside_workspace(sandboxed_dirs) -> None:
    workspace, tmp = sandboxed_dirs
    target = workspace / "out.txt"
    code = f"open({str(target)!r}, 'w').write('ok')"
    _run_sandboxed_python(code, workspace=workspace, tmp=tmp, deny_read=[])
    assert target.read_text() == "ok"


@requires_seatbelt
def test_seatbelt_allows_write_inside_tmpdir(sandboxed_dirs) -> None:
    workspace, tmp = sandboxed_dirs
    target = tmp / "scratch.txt"
    code = f"open({str(target)!r}, 'w').write('ok')"
    _run_sandboxed_python(code, workspace=workspace, tmp=tmp, deny_read=[])
    assert target.read_text() == "ok"


@requires_seatbelt
def test_seatbelt_allows_reading_normal_files(sandboxed_dirs) -> None:
    workspace, tmp = sandboxed_dirs
    normal = workspace / "normal.txt"
    normal.write_text("hello")
    code = f"print(open({str(normal)!r}).read())"
    result = _run_sandboxed_python(code, workspace=workspace, tmp=tmp, deny_read=[])
    assert "hello" in result.stdout


@requires_seatbelt
def test_seatbelt_allows_spawning_a_subprocess(sandboxed_dirs) -> None:
    workspace, tmp = sandboxed_dirs
    code = (
        "import subprocess\n"
        "r = subprocess.run(['/bin/echo', 'child_ok'], capture_output=True, text=True)\n"
        "print(r.stdout.strip())\n"
    )
    result = _run_sandboxed_python(code, workspace=workspace, tmp=tmp, deny_read=[])
    assert "child_ok" in result.stdout


@requires_seatbelt
def test_seatbelt_workspace_path_with_spaces_and_quotes(tmp_path: Path) -> None:
    # Profile-injection regression: a workspace path containing shell/Lisp
    # metacharacters must not corrupt or escape the generated profile.
    workspace = tmp_path / "weird dir 'single\" double"
    workspace.mkdir()
    tmp = tmp_path / "tmp"
    tmp.mkdir()
    target = workspace / "out.txt"
    code = f"open({str(target)!r}, 'w').write('ok')"
    result = _run_sandboxed_python(code, workspace=workspace, tmp=tmp, deny_read=[])
    assert result.returncode == 0, result.stderr
    assert target.read_text() == "ok"


# --------------------------------------------------------------------------- Real exploit-closure tests
#
# Each of these was proven exploitable by an adversarial review against the
# previous `(allow default)` + denies profile; each is re-run here, for
# real, against the new deny-by-default profile.


@requires_seatbelt
def test_seatbelt_denies_reading_other_repo_decoy_file(tmp_path: Path, sandboxed_dirs) -> None:
    workspace, tmp = sandboxed_dirs
    other_repo = tmp_path / "some-other-repo"
    other_repo.mkdir()
    decoy = other_repo / "config.py"
    decoy.write_text("SECRET = 'other-repo-secret'")
    code = f"print(open({str(decoy)!r}).read())"
    result = _run_sandboxed_python(code, workspace=workspace, tmp=tmp, deny_read=[])
    assert "other-repo-secret" not in result.stdout
    assert result.returncode != 0


@requires_seatbelt
def test_seatbelt_denies_listing_home_directory(sandboxed_dirs) -> None:
    workspace, tmp = sandboxed_dirs
    code = f"import os; print(os.listdir({str(Path.home())!r}))"
    result = _run_sandboxed_python(code, workspace=workspace, tmp=tmp, deny_read=[])
    assert result.returncode != 0
    assert "Operation not permitted" in result.stderr or "PermissionError" in result.stderr


@requires_seatbelt
def test_seatbelt_denies_reading_decoy_dotfile_outside_allowlist(
    tmp_path: Path, sandboxed_dirs
) -> None:
    workspace, tmp = sandboxed_dirs
    fake_home = tmp_path / "fake-home"
    fake_home.mkdir()
    decoy_zshrc = fake_home / ".zshrc"
    decoy_zshrc.write_text("export DECOY_SECRET=abc123")
    code = f"print(open({str(decoy_zshrc)!r}).read())"
    result = _run_sandboxed_python(code, workspace=workspace, tmp=tmp, deny_read=[])
    assert "DECOY_SECRET" not in result.stdout
    assert result.returncode != 0


@requires_seatbelt
def test_seatbelt_denies_reading_etc_passwd_is_allowed_but_ssh_keys_are_not(
    tmp_path: Path, sandboxed_dirs
) -> None:
    # Documents the accepted tradeoff from module docstring/report: /etc is
    # allowed (not secret on macOS), but a real credential path under a
    # denied home directory is not, even placed next to an allowed one.
    workspace, tmp = sandboxed_dirs
    fake_ssh = tmp_path / "fake-ssh-parent" / ".ssh"
    fake_ssh.mkdir(parents=True)
    decoy_key = fake_ssh / "id_rsa"
    decoy_key.write_text("-----BEGIN DECOY PRIVATE KEY-----")
    code = f"print(open({str(decoy_key)!r}).read())"
    result = _run_sandboxed_python(code, workspace=workspace, tmp=tmp, deny_read=[fake_ssh.parent])
    assert "DECOY PRIVATE KEY" not in result.stdout


@requires_seatbelt
def test_seatbelt_allows_multiprocessing_pool(sandboxed_dirs) -> None:
    # Needs ipc-posix-sem*/ipc-posix-shm* -- see build_seatbelt_argv's
    # docstring for why those are allowed broadly.
    workspace, tmp = sandboxed_dirs
    code = "import multiprocessing as m\nprint(m.Pool(2).map(abs, [1, -2]))\n"
    result = _run_sandboxed_python(code, workspace=workspace, tmp=tmp, deny_read=[], timeout=15)
    assert result.returncode == 0, result.stderr
    assert "[1, 2]" in result.stdout


@pytest.fixture
def decoy_keychain_item() -> str | None:
    """A throwaway generic-password item, created OUTSIDE the sandbox.

    Yields the service name, or None (and the test using it should skip) if
    `security add-generic-password` can't run non-interactively here.
    """
    import random
    import string

    suffix = "".join(random.choices(string.ascii_lowercase + string.digits, k=10))
    service = f"anymodel-sbx-test-{suffix}"
    created = subprocess.run(
        ["security", "add-generic-password", "-s", service, "-a", "test", "-w", "decoy-value"],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    if created.returncode != 0:
        yield None
    else:
        try:
            yield service
        finally:
            subprocess.run(
                ["security", "delete-generic-password", "-s", service],
                capture_output=True,
                timeout=10,
                check=False,
            )


@requires_seatbelt
def test_seatbelt_denies_keychain_access(sandboxed_dirs, decoy_keychain_item: str | None) -> None:
    if decoy_keychain_item is None:
        pytest.skip("could not create a decoy keychain item non-interactively on this machine")
    workspace, tmp = sandboxed_dirs
    argv = sandbox.build_seatbelt_argv(
        ["/usr/bin/security", "find-generic-password", "-s", decoy_keychain_item, "-w"],
        workspace=workspace,
        tmp=tmp,
        deny_read=[],
    )
    result = subprocess.run(
        argv, capture_output=True, text=True, timeout=10, check=False, cwd=str(workspace)
    )
    assert "decoy-value" not in result.stdout
    assert result.returncode != 0


@requires_seatbelt
def test_seatbelt_denies_pbpaste_and_pbcopy(sandboxed_dirs) -> None:
    workspace, tmp = sandboxed_dirs
    argv = sandbox.build_seatbelt_argv(
        ["/usr/bin/pbpaste"], workspace=workspace, tmp=tmp, deny_read=[]
    )
    result = subprocess.run(
        argv, capture_output=True, text=True, timeout=10, check=False, cwd=str(workspace)
    )
    assert result.returncode != 0

    argv2 = sandbox.build_seatbelt_argv(
        ["/bin/sh", "-c", "echo leak | /usr/bin/pbcopy"],
        workspace=workspace,
        tmp=tmp,
        deny_read=[],
    )
    result2 = subprocess.run(
        argv2, capture_output=True, text=True, timeout=10, check=False, cwd=str(workspace)
    )
    assert result2.returncode != 0


@requires_seatbelt
def test_seatbelt_denies_osascript_apple_events(sandboxed_dirs) -> None:
    # A pure computation with no application target ("1+1") doesn't need
    # Apple Events at all and succeeds even fully sandboxed -- verified
    # empirically not to be a useful automation/exfiltration primitive on
    # its own. What must fail is anything that actually *targets* a running
    # application, since that's the real automation/exfiltration channel
    # (e.g. driving a browser).
    workspace, tmp = sandboxed_dirs
    argv = sandbox.build_seatbelt_argv(
        ["/usr/bin/osascript", "-e", 'tell application "Finder" to count windows'],
        workspace=workspace,
        tmp=tmp,
        deny_read=[],
    )
    result = subprocess.run(
        argv, capture_output=True, text=True, timeout=10, check=False, cwd=str(workspace)
    )
    assert result.returncode != 0


@requires_seatbelt
def test_seatbelt_denies_open_reaching_launchservices(sandboxed_dirs) -> None:
    workspace, tmp = sandboxed_dirs
    argv = sandbox.build_seatbelt_argv(
        ["/usr/bin/open", "-g", "https://example.invalid"],
        workspace=workspace,
        tmp=tmp,
        deny_read=[],
    )
    result = subprocess.run(
        argv, capture_output=True, text=True, timeout=10, check=False, cwd=str(workspace)
    )
    # `open`'s own error text for "no handler" vs. "sandbox blocked" isn't
    # reliably distinguishable from the exit code/message alone (both are
    # non-zero), so the profile-string check above
    # (test_build_seatbelt_argv_is_deny_by_default_with_no_mach_lookup_or_network)
    # is the authoritative assertion that LaunchServices' mach services are
    # unreachable; this just confirms `open` doesn't succeed.
    assert result.returncode != 0


def test_every_deny_names_the_read_operations_not_just_the_wildcard(tmp_path: Path) -> None:
    """Seatbelt: `(deny file-read* X)` never overrides an allow that names `file-read-data`,
    in any order -- so the wildcard-only form must not come back."""
    argv = sandbox.build_seatbelt_argv(
        ["/usr/bin/true"],
        workspace=tmp_path / "ws",
        tmp=tmp_path / "tmp",
        deny_read=[tmp_path / "a", tmp_path / "ws" / "nested"],
    )
    profile = argv[argv.index("-p") + 1]
    for i in range(2):
        named = f'(deny file-read-data file-map-executable (subpath (param "DENY_READ_{i}")))'
        assert named in profile
    # The deny nested in the workspace is re-asserted AFTER the workspace re-allow.
    nested = '(deny file-read-data file-map-executable (subpath (param "DENY_READ_1")))'
    assert profile.rindex(nested) > profile.rindex(
        '(subpath (param "WORKSPACE"))\n  (subpath (param "TMP"))'
    )


@pytest.mark.skipif(sandbox.detect() != "seatbelt", reason="requires a working Seatbelt")
def test_deny_nested_in_workspace_or_extra_read_is_unreadable(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    extra = tmp_path / "extra"
    tmp = tmp_path / "tmp"
    for d in (ws / "sub", extra, tmp):
        d.mkdir(parents=True)
    (ws / "sub" / "secret").write_text("WS-SECRET")
    (ws / "ok").write_text("WS-OK")
    (extra / "secret").write_text("EXTRA-SECRET")
    (extra / "ok").write_text("EXTRA-OK")
    argv = sandbox.build_seatbelt_argv(
        ["/bin/sh", "-c", f"cat {ws}/ok {extra}/ok {ws}/sub/secret {extra}/secret"],
        workspace=ws,
        tmp=tmp,
        deny_read=[ws / "sub" / "secret", extra / "secret"],
        extra_read=(extra,),
    )
    result = subprocess.run(argv, capture_output=True, text=True, timeout=30, check=False)
    assert "WS-OK" in result.stdout and "EXTRA-OK" in result.stdout
    assert "SECRET" not in result.stdout
