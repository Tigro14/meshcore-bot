"""Tests for modules.version_info."""

import json

from modules.version_info import resolve_runtime_version


def test_main_branch_uses_baked_env_version(tmp_path, monkeypatch):
    (tmp_path / "pyproject.toml").write_text('[project]\nversion = "0.1.0"\n', encoding="utf-8")
    monkeypatch.setenv("MESHCORE_BOT_VERSION", "0.9")

    def _fake_git_run(_root, args):
        mapping = {
            ("rev-parse", "--abbrev-ref", "HEAD"): "main",
            ("rev-parse", "--short", "HEAD"): "abc1234",
            ("show", "-s", "--format=%ci", "HEAD"): "2026-04-05 10:00:00 +0000",
        }
        return mapping.get(tuple(args))

    monkeypatch.setattr("modules.version_info._safe_git_run", _fake_git_run)
    info = resolve_runtime_version(tmp_path)

    assert info["baked"] == "v0.9"
    assert info["display"] == "v0.9"
    assert info["branch"] == "main"
    assert info["commit"] == "abc1234"
    assert info["date"] == "2026-04-05"


def test_non_main_branch_uses_branch_and_commit(tmp_path, monkeypatch):
    (tmp_path / ".version_info").write_text(
        json.dumps({"installer_version": "0.9.0"}),
        encoding="utf-8",
    )

    def _fake_git_run(_root, args):
        mapping = {
            ("rev-parse", "--abbrev-ref", "HEAD"): "dev",
            ("rev-parse", "--short", "HEAD"): "fedcba9",
            ("show", "-s", "--format=%ci", "HEAD"): "2026-04-05 11:00:00 +0000",
        }
        return mapping.get(tuple(args))

    monkeypatch.setattr("modules.version_info._safe_git_run", _fake_git_run)
    info = resolve_runtime_version(tmp_path)

    assert info["baked"] == "v0.9.0"
    assert info["display"] == "dev-fedcba9"
    assert info["branch"] == "dev"


def test_baked_precedence_file_over_pyproject(tmp_path, monkeypatch):
    (tmp_path / ".version_info").write_text(
        json.dumps({"installer_version": "0.8.1"}),
        encoding="utf-8",
    )
    (tmp_path / "pyproject.toml").write_text('[project]\nversion = "0.1.0"\n', encoding="utf-8")
    monkeypatch.delenv("MESHCORE_BOT_VERSION", raising=False)
    monkeypatch.setattr("modules.version_info._safe_git_run", lambda *_args, **_kwargs: None)

    info = resolve_runtime_version(tmp_path)
    assert info["baked"] == "v0.8.1"
    assert info["display"] == "v0.8.1"


def test_fallback_to_unknown_without_baked_or_git(tmp_path, monkeypatch):
    monkeypatch.delenv("MESHCORE_BOT_VERSION", raising=False)
    monkeypatch.setattr("modules.version_info._safe_git_run", lambda *_args, **_kwargs: None)

    info = resolve_runtime_version(tmp_path)
    assert info["baked"] is None
    assert info["display"] == "unknown"


def _git_stub(mapping):
    def _fake_git_run(_root, args):
        return mapping.get(tuple(args))
    return _fake_git_run


def test_dev_checkout_does_not_report_pyproject_version_as_tag(tmp_path, monkeypatch):
    """A dev checkout bakes the in-progress version; it is not the running release.

    Reporting it as `tag` is what made the viewer footer show v1.0.0 while
    `!version` reported dev-<sha>.
    """
    (tmp_path / "pyproject.toml").write_text('[project]\nversion = "1.0.0"\n', encoding="utf-8")
    monkeypatch.delenv("MESHCORE_BOT_VERSION", raising=False)
    monkeypatch.setattr("modules.version_info._safe_git_run", _git_stub({
        ("rev-parse", "--abbrev-ref", "HEAD"): "dev",
        ("rev-parse", "--short", "HEAD"): "d7dcf91",
    }))

    info = resolve_runtime_version(tmp_path)

    assert info["display"] == "dev-d7dcf91"
    assert info["tag"] is None
    assert info["baked"] == "v1.0.0"


def test_detached_head_at_release_tag_reports_the_tag(tmp_path, monkeypatch):
    """`rev-parse --abbrev-ref` says "HEAD" when detached; that is not a branch."""
    monkeypatch.delenv("MESHCORE_BOT_VERSION", raising=False)
    monkeypatch.setattr("modules.version_info._safe_git_run", _git_stub({
        ("rev-parse", "--abbrev-ref", "HEAD"): "HEAD",
        ("rev-parse", "--short", "HEAD"): "abc1234",
        ("describe", "--tags", "--exact-match", "HEAD"): "v1.0.0",
    }))

    info = resolve_runtime_version(tmp_path)

    assert info["display"] == "v1.0.0"
    assert info["tag"] == "v1.0.0"
    assert info["branch"] is None


def test_detached_head_off_tag_reports_commit(tmp_path, monkeypatch):
    (tmp_path / "pyproject.toml").write_text('[project]\nversion = "1.0.0"\n', encoding="utf-8")
    monkeypatch.delenv("MESHCORE_BOT_VERSION", raising=False)
    monkeypatch.setattr("modules.version_info._safe_git_run", _git_stub({
        ("rev-parse", "--abbrev-ref", "HEAD"): "HEAD",
        ("rev-parse", "--short", "HEAD"): "abc1234",
    }))

    info = resolve_runtime_version(tmp_path)

    assert info["display"] == "detached-abc1234"
    assert info["tag"] is None


def test_installed_release_without_git_reports_tag(tmp_path, monkeypatch):
    """Installed releases write .version_info and have no git metadata."""
    (tmp_path / ".version_info").write_text(
        json.dumps({"installer_version": "1.0.0"}), encoding="utf-8"
    )
    monkeypatch.delenv("MESHCORE_BOT_VERSION", raising=False)
    monkeypatch.setattr("modules.version_info._safe_git_run", lambda *_a, **_kw: None)

    info = resolve_runtime_version(tmp_path)

    assert info["display"] == "v1.0.0"
    assert info["tag"] == "v1.0.0"


def test_main_branch_at_release_reports_tag(tmp_path, monkeypatch):
    """Only an *observed* tag makes this a release — note the describe stub."""
    (tmp_path / "pyproject.toml").write_text('[project]\nversion = "1.0.0"\n', encoding="utf-8")
    monkeypatch.delenv("MESHCORE_BOT_VERSION", raising=False)
    monkeypatch.setattr("modules.version_info._safe_git_run", _git_stub({
        ("rev-parse", "--abbrev-ref", "HEAD"): "main",
        ("rev-parse", "--short", "HEAD"): "abc1234",
        ("describe", "--tags", "--exact-match", "HEAD"): "v1.0.0",
    }))

    info = resolve_runtime_version(tmp_path)

    assert info["display"] == "v1.0.0"
    assert info["tag"] == "v1.0.0"


def test_main_ahead_of_tag_does_not_claim_the_release(tmp_path, monkeypatch):
    """`chore(release): prepare 1.0.0` bumps pyproject before the tag exists.

    Reporting v1.0.0 from main at that point advertises a release that has not
    been cut — the same defect as the dev-checkout footer, one branch over.
    """
    (tmp_path / "pyproject.toml").write_text('[project]\nversion = "1.0.0"\n', encoding="utf-8")
    monkeypatch.delenv("MESHCORE_BOT_VERSION", raising=False)
    monkeypatch.setattr("modules.version_info._safe_git_run", _git_stub({
        ("rev-parse", "--abbrev-ref", "HEAD"): "main",
        ("rev-parse", "--short", "HEAD"): "abc1234",
    }))

    info = resolve_runtime_version(tmp_path)

    assert info["display"] == "main-abc1234"
    assert info["tag"] is None
    assert info["baked"] == "v1.0.0"


def test_release_branch_at_tag_reports_the_tag(tmp_path, monkeypatch):
    """Not everyone releases from a branch literally named 'main'."""
    monkeypatch.delenv("MESHCORE_BOT_VERSION", raising=False)
    monkeypatch.setattr("modules.version_info._safe_git_run", _git_stub({
        ("rev-parse", "--abbrev-ref", "HEAD"): "release/1.0",
        ("rev-parse", "--short", "HEAD"): "abc1234",
        ("describe", "--tags", "--exact-match", "HEAD"): "v1.0.0",
    }))

    info = resolve_runtime_version(tmp_path)

    assert info["display"] == "v1.0.0"
    assert info["tag"] == "v1.0.0"


def test_installer_dev_version_is_not_mangled_into_a_tag(tmp_path, monkeypatch):
    """install-service.sh writes installer_version = "dev-<sha>" for untagged
    checkouts; that is not a release and must not be v-prefixed."""
    (tmp_path / ".version_info").write_text(
        json.dumps({"installer_version": "dev-abc1234"}), encoding="utf-8"
    )
    monkeypatch.delenv("MESHCORE_BOT_VERSION", raising=False)
    monkeypatch.setattr("modules.version_info._safe_git_run", lambda *_a, **_kw: None)

    info = resolve_runtime_version(tmp_path)

    assert info["display"] == "dev-abc1234"
    assert info["tag"] is None


def test_non_version_tag_name_is_not_v_prefixed(tmp_path, monkeypatch):
    monkeypatch.delenv("MESHCORE_BOT_VERSION", raising=False)
    monkeypatch.setattr("modules.version_info._safe_git_run", _git_stub({
        ("rev-parse", "--abbrev-ref", "HEAD"): "HEAD",
        ("rev-parse", "--short", "HEAD"): "abc1234",
        ("describe", "--tags", "--exact-match", "HEAD"): "nightly",
    }))

    info = resolve_runtime_version(tmp_path)

    assert info["display"] == "nightly"
    assert info["tag"] is None


def test_env_override_wins_on_a_branch_checkout(tmp_path, monkeypatch):
    """Pinning the reported version is the only reason to set the override."""
    monkeypatch.setenv("MESHCORE_BOT_VERSION", "9.9.9")
    monkeypatch.setattr("modules.version_info._safe_git_run", _git_stub({
        ("rev-parse", "--abbrev-ref", "HEAD"): "feat/thing",
        ("rev-parse", "--short", "HEAD"): "abc1234",
        ("describe", "--tags", "--exact-match", "HEAD"): "v1.0.0",
    }))

    info = resolve_runtime_version(tmp_path)

    assert info["display"] == "v9.9.9"
    assert info["tag"] == "v9.9.9"
