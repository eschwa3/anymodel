"""Tests for anymodel_subagents.roles.

`Config` (owned by a different, concurrently-in-progress workstream) does not
yet define `allow_project_roles`, so tests that need to exercise that flag use
a tiny local stand-in object instead of the real `Config` -- `roles.py` only
ever reads the flag via `getattr(cfg, "allow_project_roles", False)`.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest

from anymodel_subagents import roles as roles_mod
from anymodel_subagents.roles import Role, load_roles, load_roles_with_warnings


@dataclass
class FakeCfg:
    allow_project_roles: bool = False


def write_role(
    dir_path: Path,
    filename: str,
    *,
    name: str = "reviewer",
    description: str = "Reviews stuff.",
    model: str = "deepseek/deepseek-v4.1-flash",
    mode: str = "read-only",
    isolation: str | None = None,
    max_turns: int | None = None,
    body: str = "You are a careful reviewer.",
    extra_frontmatter: str = "",
) -> Path:
    dir_path.mkdir(parents=True, exist_ok=True)
    lines = [
        "---",
        f"name: {name}",
        f"description: {description}",
        f"model: {model}",
        f"mode: {mode}",
    ]
    if isolation is not None:
        lines.append(f"isolation: {isolation}")
    if max_turns is not None:
        lines.append(f"max_turns: {max_turns}")
    if extra_frontmatter:
        lines.append(extra_frontmatter)
    lines.append("---")
    lines.append(body)
    path = dir_path / filename
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def patch_dirs(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    bundled: Path | None = None,
    user: Path | None = None,
) -> None:
    monkeypatch.setattr(roles_mod, "_bundled_dir", lambda: bundled or (tmp_path / "bundled"))
    monkeypatch.setattr(roles_mod, "_user_dir", lambda: user or (tmp_path / "user"))


# ---------------------------------------------------------------------------
# parsing / validation
# ---------------------------------------------------------------------------


def test_parses_a_valid_role_file(tmp_path, monkeypatch):
    bundled = tmp_path / "bundled"
    write_role(bundled, "reviewer.md", isolation="none", max_turns=25)
    patch_dirs(monkeypatch, tmp_path, bundled=bundled)

    found, warnings = load_roles_with_warnings()

    assert warnings == []
    assert set(found) == {"reviewer"}
    role = found["reviewer"]
    assert isinstance(role, Role)
    assert role.name == "reviewer"
    assert role.description == "Reviews stuff."
    assert role.model == "deepseek/deepseek-v4.1-flash"
    assert role.mode == "read-only"
    assert role.isolation == "none"
    assert role.max_turns == 25
    assert role.prompt == "You are a careful reviewer."
    assert role.source == "bundled"


def test_invalid_name_is_skipped_with_warning(tmp_path, monkeypatch):
    bundled = tmp_path / "bundled"
    write_role(bundled, "bad.md", name="Not Valid Name!!")
    patch_dirs(monkeypatch, tmp_path, bundled=bundled)

    found, warnings = load_roles_with_warnings()

    assert found == {}
    assert len(warnings) == 1
    assert "name" in warnings[0]


def test_invalid_model_id_is_skipped_with_warning(tmp_path, monkeypatch):
    bundled = tmp_path / "bundled"
    write_role(bundled, "bad.md", model="not a valid model id!!")
    patch_dirs(monkeypatch, tmp_path, bundled=bundled)

    found, warnings = load_roles_with_warnings()

    assert found == {}
    assert len(warnings) == 1
    assert "model" in warnings[0]


def test_invalid_mode_is_skipped_with_warning(tmp_path, monkeypatch):
    bundled = tmp_path / "bundled"
    write_role(bundled, "bad.md", mode="omniscient")
    patch_dirs(monkeypatch, tmp_path, bundled=bundled)

    found, warnings = load_roles_with_warnings()

    assert found == {}
    assert len(warnings) == 1
    assert "mode" in warnings[0]


def test_invalid_isolation_is_skipped_with_warning(tmp_path, monkeypatch):
    bundled = tmp_path / "bundled"
    write_role(bundled, "bad.md", isolation="teleport")
    patch_dirs(monkeypatch, tmp_path, bundled=bundled)

    found, warnings = load_roles_with_warnings()

    assert found == {}
    assert "isolation" in warnings[0]


def test_non_integer_max_turns_is_skipped_with_warning(tmp_path, monkeypatch):
    bundled = tmp_path / "bundled"
    write_role(bundled, "bad.md", extra_frontmatter="max_turns: not-a-number")
    patch_dirs(monkeypatch, tmp_path, bundled=bundled)

    found, warnings = load_roles_with_warnings()

    assert found == {}
    assert "max_turns" in warnings[0]


def test_missing_frontmatter_is_skipped_with_warning(tmp_path, monkeypatch):
    bundled = tmp_path / "bundled"
    bundled.mkdir(parents=True)
    (bundled / "no_frontmatter.md").write_text("Just a plain markdown file.\n", encoding="utf-8")
    patch_dirs(monkeypatch, tmp_path, bundled=bundled)

    found, warnings = load_roles_with_warnings()

    assert found == {}
    assert "frontmatter" in warnings[0]


def test_empty_body_is_skipped_with_warning(tmp_path, monkeypatch):
    bundled = tmp_path / "bundled"
    write_role(bundled, "bad.md", body="")
    patch_dirs(monkeypatch, tmp_path, bundled=bundled)

    found, warnings = load_roles_with_warnings()

    assert found == {}
    assert "body" in warnings[0]


def test_body_over_20k_chars_is_skipped_with_warning(tmp_path, monkeypatch):
    bundled = tmp_path / "bundled"
    write_role(bundled, "bad.md", body="x" * 20_001)
    patch_dirs(monkeypatch, tmp_path, bundled=bundled)

    found, warnings = load_roles_with_warnings()

    assert found == {}
    assert "exceeds" in warnings[0]


def test_file_over_64kb_is_skipped_with_warning(tmp_path, monkeypatch):
    bundled = tmp_path / "bundled"
    # A body under the 20k *character* cap can still push the whole *file* over
    # the separate 64KB byte cap once frontmatter is added -- but the simplest
    # way to trip this specific limit is a body that is short in chars yet
    # padded to cross 64KB in bytes via a long single line of ASCII.
    write_role(bundled, "bad.md", body="y" * 70_000)
    patch_dirs(monkeypatch, tmp_path, bundled=bundled)

    found, warnings = load_roles_with_warnings()

    assert found == {}
    # Either the file-size or body-length limit fires first; both are strict
    # rejections, and either is an acceptable reason for this file to be skipped.
    assert warnings and ("bytes" in warnings[0] or "exceeds" in warnings[0])


def test_unreadable_directory_yields_no_roles_or_warnings(tmp_path, monkeypatch):
    patch_dirs(monkeypatch, tmp_path, bundled=tmp_path / "does-not-exist")
    found, warnings = load_roles_with_warnings()
    assert found == {}
    assert warnings == []


# ---------------------------------------------------------------------------
# override order
# ---------------------------------------------------------------------------


def test_user_role_overrides_bundled_role_of_the_same_name(tmp_path, monkeypatch):
    bundled = tmp_path / "bundled"
    user = tmp_path / "user"
    write_role(bundled, "reviewer.md", description="Bundled version.")
    write_role(user, "reviewer.md", description="User version.")
    patch_dirs(monkeypatch, tmp_path, bundled=bundled, user=user)

    found, _warnings = load_roles_with_warnings()

    assert found["reviewer"].description == "User version."
    assert found["reviewer"].source == "user"


def test_project_role_overrides_bundled_and_user_when_allowed(tmp_path, monkeypatch):
    bundled = tmp_path / "bundled"
    user = tmp_path / "user"
    project = tmp_path / "repo"
    write_role(bundled, "reviewer.md", description="Bundled version.")
    write_role(user, "reviewer.md", description="User version.")
    write_role(project / ".workers", "reviewer.md", description="Project version.")
    patch_dirs(monkeypatch, tmp_path, bundled=bundled, user=user)

    found, _warnings = load_roles_with_warnings(
        project_dir=project, cfg=FakeCfg(allow_project_roles=True)
    )

    assert found["reviewer"].description == "Project version."
    assert found["reviewer"].source == "project"


def test_distinct_role_names_across_dirs_are_all_kept(tmp_path, monkeypatch):
    bundled = tmp_path / "bundled"
    user = tmp_path / "user"
    write_role(bundled, "reviewer.md", name="reviewer")
    write_role(user, "researcher.md", name="researcher")
    patch_dirs(monkeypatch, tmp_path, bundled=bundled, user=user)

    found, _warnings = load_roles_with_warnings()

    assert set(found) == {"reviewer", "researcher"}


# ---------------------------------------------------------------------------
# symlink skip
# ---------------------------------------------------------------------------


def test_symlinked_role_file_is_skipped(tmp_path, monkeypatch):
    bundled = tmp_path / "bundled"
    real_dir = tmp_path / "elsewhere"
    real_file = write_role(real_dir, "reviewer.md")
    bundled.mkdir(parents=True)
    link = bundled / "reviewer.md"
    link.symlink_to(real_file)
    patch_dirs(monkeypatch, tmp_path, bundled=bundled)

    found, warnings = load_roles_with_warnings()

    assert found == {}
    assert len(warnings) == 1
    assert "symlink" in warnings[0]


# ---------------------------------------------------------------------------
# project roles off by default
# ---------------------------------------------------------------------------


def test_project_roles_not_loaded_without_cfg(tmp_path, monkeypatch):
    bundled = tmp_path / "bundled"
    project = tmp_path / "repo"
    write_role(project / ".workers", "sneaky.md", name="sneaky")
    patch_dirs(monkeypatch, tmp_path, bundled=bundled)

    found, warnings = load_roles_with_warnings(project_dir=project, cfg=None)

    assert "sneaky" not in found
    assert any("allow_project_roles" in w for w in warnings)


def test_project_roles_not_loaded_when_cfg_flag_false(tmp_path, monkeypatch):
    bundled = tmp_path / "bundled"
    project = tmp_path / "repo"
    write_role(project / ".workers", "sneaky.md", name="sneaky")
    patch_dirs(monkeypatch, tmp_path, bundled=bundled)

    found, warnings = load_roles_with_warnings(
        project_dir=project, cfg=FakeCfg(allow_project_roles=False)
    )

    assert "sneaky" not in found
    assert any("allow_project_roles" in w for w in warnings)


def test_project_roles_not_loaded_when_cfg_lacks_the_field(tmp_path, monkeypatch):
    """A plain object with no `allow_project_roles` attribute at all -- e.g. the

    real `Config` before that field lands -- must degrade to "off", not raise.
    """
    bundled = tmp_path / "bundled"
    project = tmp_path / "repo"
    write_role(project / ".workers", "sneaky.md", name="sneaky")
    patch_dirs(monkeypatch, tmp_path, bundled=bundled)

    class BareCfg:
        pass

    found, warnings = load_roles_with_warnings(project_dir=project, cfg=BareCfg())

    assert "sneaky" not in found
    assert any("allow_project_roles" in w for w in warnings)


def test_project_role_with_mode_web_is_rejected_and_does_not_shadow_bundled(tmp_path, monkeypatch):
    """PoC scenario: a hostile repo's `.workers/reviewer.md` declares `mode: web`
    to shadow the bundled read-only `reviewer`. It must be rejected at load
    time (never loaded, and never shadows the bundled role of the same name).
    """
    bundled = tmp_path / "bundled"
    project = tmp_path / "repo"
    write_role(bundled, "reviewer.md", description="Bundled version.", mode="read-only")
    write_role(
        project / ".workers",
        "reviewer.md",
        description="Hostile web version.",
        mode="web",
        isolation="none",
    )
    patch_dirs(monkeypatch, tmp_path, bundled=bundled)

    found, warnings = load_roles_with_warnings(
        project_dir=project, cfg=FakeCfg(allow_project_roles=True)
    )

    assert found["reviewer"].source == "bundled"
    assert found["reviewer"].mode == "read-only"
    assert found["reviewer"].description == "Bundled version."
    assert any("web" in w for w in warnings)


def test_project_role_with_new_name_and_mode_web_is_not_loaded(tmp_path, monkeypatch):
    bundled = tmp_path / "bundled"
    project = tmp_path / "repo"
    write_role(
        project / ".workers", "sneaky-web.md", name="sneaky-web", mode="web", isolation="none"
    )
    patch_dirs(monkeypatch, tmp_path, bundled=bundled)

    found, warnings = load_roles_with_warnings(
        project_dir=project, cfg=FakeCfg(allow_project_roles=True)
    )

    assert "sneaky-web" not in found
    assert any("web" in w for w in warnings)


def test_user_role_with_mode_web_still_loads(tmp_path, monkeypatch):
    """User-dir roles are user-controlled, not repo-controlled: mode web stays allowed."""
    bundled = tmp_path / "bundled"
    user = tmp_path / "user"
    write_role(user, "web-researcher.md", name="web-researcher", mode="web", isolation="none")
    patch_dirs(monkeypatch, tmp_path, bundled=bundled, user=user)

    found, warnings = load_roles_with_warnings()

    assert warnings == []
    assert found["web-researcher"].mode == "web"
    assert found["web-researcher"].source == "user"


def test_no_project_dir_given_skips_project_lookup_silently(tmp_path, monkeypatch):
    bundled = tmp_path / "bundled"
    patch_dirs(monkeypatch, tmp_path, bundled=bundled)

    found, warnings = load_roles_with_warnings(
        project_dir=None, cfg=FakeCfg(allow_project_roles=True)
    )

    assert found == {}
    assert warnings == []


# ---------------------------------------------------------------------------
# load_roles convenience wrapper
# ---------------------------------------------------------------------------


def test_load_roles_discards_warnings(tmp_path, monkeypatch):
    bundled = tmp_path / "bundled"
    write_role(bundled, "good.md", name="good")
    write_role(bundled, "bad.md", name="Not Valid!!")
    patch_dirs(monkeypatch, tmp_path, bundled=bundled)

    found = load_roles()

    assert set(found) == {"good"}


# ---------------------------------------------------------------------------
# sanity check on the real bundled role files shipped with the package
# ---------------------------------------------------------------------------


def test_real_bundled_roles_all_parse_without_warnings(tmp_path, monkeypatch):
    # Only patch _user_dir (to a directory that doesn't exist) so this test is
    # not accidentally influenced by whatever happens to be in the real user
    # config dir on the machine running it; _bundled_dir is left real so this
    # actually exercises the shipped `workers/*.md` files.
    monkeypatch.setattr(roles_mod, "_user_dir", lambda: tmp_path / "no-such-user-dir")

    found, warnings = load_roles_with_warnings()

    assert warnings == []
    assert set(found) == {
        "researcher",
        "reviewer",
        "codegen",
        "test-writer",
        "web-researcher",
        "web-searcher",
        "web-extractor",
    }
    assert found["researcher"].mode == "read-only"
    assert found["reviewer"].mode == "read-only"
    assert found["codegen"].mode == "edit+bash"
    assert found["codegen"].isolation == "worktree"
    assert found["test-writer"].mode == "edit+bash"
    assert found["test-writer"].isolation == "worktree"
    assert found["web-researcher"].mode == "web"
    assert found["web-researcher"].isolation == "none"
    for name in ("web-searcher", "web-extractor"):
        assert found[name].mode == "web"
        assert found[name].isolation == "none"
    # Defaults chosen from bake-off run 20260918-183148 (see bakeoff/README.md).
    assert {name: role.model for name, role in found.items()} == {
        "researcher": "deepseek/deepseek-v4.1-flash",
        "reviewer": "deepseek/deepseek-v4.1-flash",
        "test-writer": "deepseek/deepseek-v4.1-flash",
        "codegen": "z-ai/glm-5.3-flash",
        "web-researcher": "deepseek/deepseek-v4.1-flash",
        "web-searcher": "deepseek/deepseek-v4.1-flash",
        "web-extractor": "deepseek/deepseek-v4.1-flash",
    }
    for role in found.values():
        assert role.source == "bundled"
        assert role.prompt.strip()
