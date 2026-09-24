"""Tests for web_denylist.py: the parser, the matcher, bundled/user/config
merging (load_denylist), and the Claude Code rule exporter.

Format/matching semantics are covered primarily by replaying
tests/data/denylist_vectors.txt -- a harness-neutral fixture any other
implementation of the format (see web_denylist.py's module docstring) can
replay too.
"""

from __future__ import annotations

import argparse
import importlib.resources
import json
from pathlib import Path

import pytest

from anymodel_subagents.web_denylist import (
    Denylist,
    load_denylist,
    parse,
    to_claude_code_rules,
)

VECTORS_PATH = Path(__file__).parent / "data" / "denylist_vectors.txt"


def _load_vectors(path: Path) -> tuple[str, list[tuple[str, bool]]]:
    """Parse the `[list]` / `[cases]` vectors file described at its own top."""
    section: str | None = None
    list_lines: list[str] = []
    cases: list[tuple[str, bool]] = []
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        stripped = raw_line.strip()
        if stripped == "[list]":
            section = "list"
            continue
        if stripped == "[cases]":
            section = "cases"
            continue
        if section is None:
            continue
        if section == "list":
            list_lines.append(raw_line)
        else:
            if not stripped or stripped.startswith("#"):
                continue
            host, verdict = stripped.split()
            if verdict not in ("allow", "deny"):
                raise AssertionError(f"bad vectors line: {raw_line!r}")
            cases.append((host, verdict == "deny"))
    return "\n".join(list_lines), cases


# ---------------------------------------------------------------------------
# Portable vectors
# ---------------------------------------------------------------------------


def test_vectors_file_has_cases() -> None:
    _, cases = _load_vectors(VECTORS_PATH)
    assert len(cases) >= 15


@pytest.mark.parametrize("host, expect_deny", _load_vectors(VECTORS_PATH)[1])
def test_vectors(host: str, expect_deny: bool) -> None:
    list_text, _ = _load_vectors(VECTORS_PATH)
    denylist = Denylist(parse(list_text))
    assert denylist.is_blocked(host) is expect_deny, host


# ---------------------------------------------------------------------------
# parse(): valid lines, comments, blanks
# ---------------------------------------------------------------------------


def test_parse_ignores_blank_lines_and_comments() -> None:
    text = "\n# a full-line comment\n\nexample.com  # trailing comment\n"
    assert parse(text) == ["example.com"]


def test_parse_exception_prefix_preserved() -> None:
    assert parse("!sub.example.com\n") == ["!sub.example.com"]


def test_parse_lowercases_and_strips_trailing_dot() -> None:
    assert parse("EXAMPLE.COM.\n") == ["example.com"]


def test_parse_idna_normalizes_unicode() -> None:
    assert parse("müxample.com\n") == ["xn--mxample-n2a.com"]


# ---------------------------------------------------------------------------
# parse(): invalid lines -> ValueError naming the line number
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad_line, reason_snippet",
    [
        ("https://example.com", "scheme"),
        ("example.com/path", "scheme"),
        ("example.com:443", "port"),
        ("1.2.3.4", "IP literal"),
        ("*.example.com", "wildcard"),
        ("exa mple.com", "whitespace"),
        ("localhost", "single-label"),
        ("!", "empty domain"),
        ("a..b.com", "empty label"),
        (".example.com", "empty label"),
    ],
)
def test_parse_rejects_invalid_line(bad_line: str, reason_snippet: str) -> None:
    text = f"example.com\n{bad_line}\n"
    with pytest.raises(ValueError, match="line 2") as exc:
        parse(text)
    assert reason_snippet.split()[0].lower() in str(exc.value).lower()


def test_parse_error_names_correct_line_number() -> None:
    text = "example.com\nother.example\n*.bad.com\n"
    with pytest.raises(ValueError, match="line 3"):
        parse(text)


# ---------------------------------------------------------------------------
# Denylist(): same validation, plus matching semantics
# ---------------------------------------------------------------------------


def test_denylist_constructor_rejects_invalid_entry() -> None:
    with pytest.raises(ValueError, match="invalid denylist entry"):
        Denylist(["not a domain with spaces"])


def test_denylist_apex_blocks_subdomains_not_lookalikes() -> None:
    dl = Denylist(["example.com"])
    assert dl.is_blocked("example.com")
    assert dl.is_blocked("a.b.example.com")
    assert not dl.is_blocked("notexample.com")


def test_denylist_same_domain_deny_beats_exception() -> None:
    dl = Denylist(["example.com", "!example.com"])
    assert dl.is_blocked("example.com")


def test_denylist_longest_match_wins() -> None:
    dl = Denylist(["example.org", "!safe.example.org", "evil.safe.example.org"])
    assert dl.is_blocked("example.org")
    assert not dl.is_blocked("safe.example.org")
    assert not dl.is_blocked("sub.safe.example.org")
    assert dl.is_blocked("evil.safe.example.org")


# ---------------------------------------------------------------------------
# is_blocked(): fail-closed on garbage hosts
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "garbage_host",
    ["localhost", "1.2.3.4", "http://example.com", "*.example.com", "not a host", ""],
)
def test_is_blocked_fails_closed_on_garbage(garbage_host: str) -> None:
    dl = Denylist(["example.com"])
    assert dl.is_blocked(garbage_host) is True


def test_is_blocked_allows_ordinary_unlisted_host() -> None:
    dl = Denylist(["example.com"])
    assert dl.is_blocked("anthropic.com") is False


# ---------------------------------------------------------------------------
# Bundled file
# ---------------------------------------------------------------------------


def test_bundled_denylist_parses_and_is_nonempty() -> None:
    text = (
        importlib.resources.files("anymodel_subagents")
        .joinpath("web-denylist.txt")
        .read_text(encoding="utf-8")
    )
    entries = parse(text)
    assert len(entries) >= 40
    assert "webhook.site" in entries


def test_bundled_denylist_blocks_a_known_sink() -> None:
    dl = Denylist(
        parse(
            importlib.resources.files("anymodel_subagents")
            .joinpath("web-denylist.txt")
            .read_text(encoding="utf-8")
        )
    )
    assert dl.is_blocked("webhook.site")
    assert dl.is_blocked("sub.webhook.site")
    assert not dl.is_blocked("anthropic.com")


# ---------------------------------------------------------------------------
# load_denylist(): bundled + user file (XDG config dir) + extra
# ---------------------------------------------------------------------------


def _point_config_at(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point config_path() at `tmp_path/config.yaml` (need not exist) so
    config_path().parent / "web-denylist.txt" is a predictable, isolated user
    denylist location for these tests.
    """
    cfg_dir = tmp_path / "cfgdir"
    cfg_dir.mkdir()
    monkeypatch.setenv("ANYMODEL_CONFIG", str(cfg_dir / "config.yaml"))
    return cfg_dir


def test_load_denylist_missing_user_file_is_fine(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _point_config_at(tmp_path, monkeypatch)
    dl = load_denylist()
    assert dl.is_blocked("webhook.site")


def test_load_denylist_merges_user_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg_dir = _point_config_at(tmp_path, monkeypatch)
    (cfg_dir / "web-denylist.txt").write_text("myuserdomain.example\n")

    dl = load_denylist()

    assert dl.is_blocked("myuserdomain.example")
    assert dl.is_blocked("sub.myuserdomain.example")
    assert dl.is_blocked("webhook.site")  # bundled entries still present


def test_load_denylist_invalid_user_file_raises_with_path_and_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg_dir = _point_config_at(tmp_path, monkeypatch)
    user_file = cfg_dir / "web-denylist.txt"
    user_file.write_text("example.com\nnot a valid entry\n")

    with pytest.raises(ValueError) as exc:
        load_denylist()

    assert str(user_file) in str(exc.value)
    assert "line 2" in str(exc.value)


def test_load_denylist_extra_entries(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _point_config_at(tmp_path, monkeypatch)
    dl = load_denylist(extra=["extra-exfil.example"])
    assert dl.is_blocked("extra-exfil.example")
    assert dl.is_blocked("sub.extra-exfil.example")


# ---------------------------------------------------------------------------
# to_claude_code_rules()
# ---------------------------------------------------------------------------


def test_to_claude_code_rules_emits_apex_and_wildcard() -> None:
    dl = Denylist(["evil.example"])
    rules, warnings = to_claude_code_rules(dl)
    assert rules == ["WebFetch(domain:*.evil.example)", "WebFetch(domain:evil.example)"]
    assert warnings == []


def test_to_claude_code_rules_skips_entry_shadowed_by_exception() -> None:
    dl = Denylist(["evil.example", "!safe.evil.example"])
    rules, warnings = to_claude_code_rules(dl)
    assert rules == []
    assert len(warnings) == 1
    assert "evil.example" in warnings[0]
    assert "safe.evil.example" in warnings[0]


def test_to_claude_code_rules_sorted_and_deduped() -> None:
    dl = Denylist(["b.example", "a.example"])
    rules, _ = to_claude_code_rules(dl)
    assert rules == sorted(rules)
    assert len(rules) == len(set(rules))


# ---------------------------------------------------------------------------
# CLI (`anymodel-worker denylist`)
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolated_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for var in ("ANYMODEL_CONFIG", "XDG_CONFIG_HOME"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("ANYMODEL_CONFIG", str(tmp_path / "_no_such_config.yaml"))


def test_cli_denylist_txt_format(capsys: pytest.CaptureFixture[str]) -> None:
    from anymodel_subagents import cli

    rc = cli._denylist_command(argparse.Namespace(format="txt"))

    assert rc == 0
    out_lines = capsys.readouterr().out.splitlines()
    assert "webhook.site" in out_lines


def test_cli_denylist_claude_code_format(capsys: pytest.CaptureFixture[str]) -> None:
    from anymodel_subagents import cli

    rc = cli._denylist_command(argparse.Namespace(format="claude-code"))

    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    rules = payload["permissions"]["deny"]
    assert "WebFetch(domain:webhook.site)" in rules
    assert "WebFetch(domain:*.webhook.site)" in rules


def test_cli_denylist_invalid_config_returns_2(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from anymodel_subagents import cli

    bad_config = tmp_path / "bad_config.yaml"
    bad_config.write_text("not_a_real_key: 1\n")
    monkeypatch.setenv("ANYMODEL_CONFIG", str(bad_config))

    rc = cli._denylist_command(argparse.Namespace(format="txt"))

    assert rc == 2
    assert "invalid config" in capsys.readouterr().err.lower()


def test_cli_denylist_warns_on_shadowed_exception_from_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from anymodel_subagents import cli

    config = tmp_path / "config.yaml"
    config.write_text("web_denylist_extra:\n  - '!sub.pastebin.com'\n")
    monkeypatch.setenv("ANYMODEL_CONFIG", str(config))

    rc = cli._denylist_command(argparse.Namespace(format="claude-code"))

    assert rc == 0
    err = capsys.readouterr().err
    assert "pastebin.com" in err
    assert "sub.pastebin.com" in err


def test_build_parser_denylist_defaults() -> None:
    from anymodel_subagents import cli

    args = cli._build_parser().parse_args(["denylist"])
    assert args.command == "denylist"
    assert args.format == "txt"


def test_build_parser_denylist_claude_code_format() -> None:
    from anymodel_subagents import cli

    args = cli._build_parser().parse_args(["denylist", "--format", "claude-code"])
    assert args.format == "claude-code"


def test_build_parser_denylist_rejects_bad_format() -> None:
    from anymodel_subagents import cli

    with pytest.raises(SystemExit):
        cli._build_parser().parse_args(["denylist", "--format", "yaml"])


def test_main_denylist_command_exits_0(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import sys

    from anymodel_subagents import cli

    monkeypatch.setattr(sys, "argv", ["anymodel-worker", "denylist"])

    with pytest.raises(SystemExit) as exc:
        cli.main()

    assert exc.value.code == 0
    assert "webhook.site" in capsys.readouterr().out
