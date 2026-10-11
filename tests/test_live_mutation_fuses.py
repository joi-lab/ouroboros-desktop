import pathlib

import pytest


def test_live_settings_and_repo_mutation_fuses_refuse_pytest_access(monkeypatch):
    from ouroboros import config as cfg
    from supervisor import git_ops

    live_settings = pathlib.Path.home() / "Ouroboros" / "data" / "settings.json"
    monkeypatch.setattr(cfg, "SETTINGS_PATH", live_settings, raising=True)
    monkeypatch.delenv("OUROBOROS_ALLOW_LIVE_DATA_TESTS", raising=False)

    with pytest.raises(RuntimeError, match="Refusing to write live Ouroboros settings"):
        cfg.save_settings({"OUROBOROS_RUNTIME_MODE": "advanced"})

    monkeypatch.setattr(git_ops, "REPO_DIR", pathlib.Path.home() / "Ouroboros" / "repo")
    monkeypatch.delenv("OUROBOROS_ALLOW_LIVE_REPO_TESTS", raising=False)

    with pytest.raises(RuntimeError, match="destructive git reset/clean"):
        git_ops._guard_live_repo_destructive_git(["git", "reset", "--hard", "HEAD"])
    with pytest.raises(RuntimeError, match="destructive git reset/clean"):
        git_ops._guard_live_repo_destructive_git(["git", "clean", "-fd"])
