"""Golden capability tests — product promises exercised from zero, so a silent capability
regression turns a test red. Flagship: light can CREATE a new external skill from scratch (no
pre-mkdir payload), the exact behavior the f705b37 blanket is_dir gate regressed."""

from ouroboros.contracts.skill_payload_policy import decide_payload_short_form


def test_light_creates_new_external_skill_from_scratch(tmp_path):
    """A NEW external skill is created by writing its SKILL.md/skill.json manifest even though the
    payload directory does not exist yet (the authoring signal). A non-manifest file into a missing
    payload still errors (typo guard); an existing payload edits normally. Red on f705b37."""
    drive_root = tmp_path / "data"
    repo_dir = tmp_path / "repo"
    drive_root.mkdir(parents=True)
    repo_dir.mkdir(parents=True)

    # 1. CREATE: payload does NOT exist, the write IS the manifest -> allowed (constraint, no error).
    for manifest in ("SKILL.md", "skill.json"):
        decision = decide_payload_short_form(
            bucket="external", skill_name="weather", path_text=manifest,
            repo_dir=repo_dir, drive_root=drive_root,
        )
        assert decision.error == "", f"writing {manifest} into a fresh payload must create, not error"
        assert decision.constraint is not None and decision.constraint.mode == "skill_repair"

    # 2. TYPO GUARD: payload does NOT exist, the write is a non-manifest file -> still errors.
    typo = decide_payload_short_form(
        bucket="external", skill_name="weather", path_text="plugin.py",
        repo_dir=repo_dir, drive_root=drive_root,
    )
    assert "skill payload not found" in (typo.error or "")
    assert typo.constraint is None

    # 3. EDIT: an EXISTING payload edits normally, any filename.
    (drive_root / "skills" / "external" / "weather").mkdir(parents=True)
    edit = decide_payload_short_form(
        bucket="external", skill_name="weather", path_text="plugin.py",
        repo_dir=repo_dir, drive_root=drive_root,
    )
    assert edit.error == "" and edit.constraint is not None

    # 4. MARKETPLACE buckets are INSTALLED, not authored from scratch: a manifest into a missing
    #    clawhub/ouroboroshub payload still errors (only `external` is the agent-authoring bucket).
    for marketplace in ("clawhub", "ouroboroshub"):
        market = decide_payload_short_form(
            bucket=marketplace, skill_name="fromhub", path_text="SKILL.md",
            repo_dir=repo_dir, drive_root=drive_root,
        )
        assert "skill payload not found" in (market.error or ""), f"{marketplace} create must error"
        assert market.constraint is None


def test_skill_create_signal_only_fires_for_root_manifest():
    """The CREATE carve-out must be tight: only the manifest at the payload ROOT is an authoring
    signal — a nested or absolute path ending in SKILL.md is NOT, so it cannot smuggle a write into
    data/skills/<bucket>/<skill>/nested/ on a missing payload."""
    from ouroboros.contracts.skill_payload_policy import _is_skill_create_signal

    assert _is_skill_create_signal("SKILL.md") is True
    assert _is_skill_create_signal("skill.json") is True
    assert _is_skill_create_signal("./SKILL.md") is True
    assert _is_skill_create_signal("nested/SKILL.md") is False
    assert _is_skill_create_signal("/etc/SKILL.md") is False
    assert _is_skill_create_signal("plugin.py") is False
    assert _is_skill_create_signal("") is False


def test_short_form_create_marks_new_skill_self_authored(tmp_path, monkeypatch):
    """End-to-end: creating a new external skill via the bucket+skill_name short-form (SKILL.md into
    a non-existent payload) succeeds AND marks it self-authored. The short-form synthesizes a
    skill_repair constraint, so provenance marking must NOT be suppressed for a genuine create
    (target/marker absent) — only for repair of an existing skill."""
    from ouroboros import config
    from ouroboros.tools.registry import ToolContext, ToolRegistry

    repo_dir = tmp_path / "repo"
    drive_root = tmp_path / "drive"
    repo_dir.mkdir()
    drive_root.mkdir()
    # the self-authored marker keys off the GLOBAL config DATA_DIR — align it with this drive.
    monkeypatch.setattr(config, "DATA_DIR", str(drive_root))
    ctx = ToolContext(repo_dir=repo_dir, drive_root=drive_root)
    registry = ToolRegistry(repo_dir=repo_dir, drive_root=drive_root)
    registry._ctx = ctx

    result = registry.execute("write_file", {
        "root": "skill_payload", "bucket": "external", "skill_name": "fresh",
        "path": "SKILL.md", "content": "---\nname: fresh\n---\nA fresh skill.\n",
    })

    assert "OK" in result and "ERROR" not in result and "BLOCKED" not in result, result
    payload = drive_root / "skills" / "external" / "fresh"
    assert (payload / "SKILL.md").exists(), "the manifest must be created"
    assert (payload / ".self_authored.json").exists(), "a newly created external skill must be marked self-authored"


def test_runtime_data_explicit_path_applies_manifest_first_typo_guard(tmp_path, monkeypatch):
    """The explicit runtime_data skills/<bucket>/<skill>/... write path applies the SAME manifest-first
    typo guard as the bucket/skill_name short-form (SSOT is_skill_create_typo). Closes the
    write_file(root=runtime_data, path=skills/external/new/plugin.py) bypass the cumulative review found:
    a non-manifest into a MISSING external payload is a typo (error, nothing mkdir'd); the root manifest
    of a NEW external skill creates; an EXISTING payload writes any filename; a missing marketplace
    payload errors (install, don't author)."""
    from ouroboros import config
    from ouroboros.tools.registry import ToolContext, ToolRegistry

    repo_dir = tmp_path / "repo"
    drive_root = tmp_path / "drive"
    repo_dir.mkdir()
    drive_root.mkdir()
    monkeypatch.setattr(config, "DATA_DIR", str(drive_root))
    monkeypatch.setattr("ouroboros.safety.check_safety", lambda *a, **k: (True, ""))
    ctx = ToolContext(repo_dir=repo_dir, drive_root=drive_root)
    registry = ToolRegistry(repo_dir=repo_dir, drive_root=drive_root)
    registry._ctx = ctx

    def _write(path, content):
        return registry.execute("write_file", {"root": "runtime_data", "path": path, "content": content})

    # 1. TYPO: a non-manifest into a MISSING external payload is blocked; nothing is mkdir'd.
    typo = _write("skills/external/newskill/plugin.py", "code")
    assert "DATA_WRITE_ERROR" in typo and "skill payload not found" in typo, typo
    assert not (drive_root / "skills" / "external" / "newskill").exists(), "a typo must not mkdir a payload"

    # 2. CREATE: the root manifest of a NEW external skill is the authoring signal -> allowed.
    created = _write("skills/external/newskill/SKILL.md", "---\nname: newskill\n---\nNew.\n")
    assert "ERROR" not in created and "BLOCKED" not in created, created
    assert (drive_root / "skills" / "external" / "newskill" / "SKILL.md").is_file()

    # 3. EDIT: an EXISTING payload accepts any filename (the guard only fires for a MISSING payload).
    edit = _write("skills/external/newskill/plugin.py", "code")
    assert "ERROR" not in edit and "BLOCKED" not in edit, edit
    assert (drive_root / "skills" / "external" / "newskill" / "plugin.py").is_file()

    # 4. MARKETPLACE: a missing clawhub/ouroboroshub payload is installed, never authored -> error.
    market = _write("skills/clawhub/fromhub/SKILL.md", "# hub")
    assert "DATA_WRITE_ERROR" in market and "skill payload not found" in market, market

    # 5. NORMALIZED-PREFIX BYPASS: a redundant drive-root prefix must NOT smuggle past the guard —
    #    _normalize_data_read_path strips it to skills/..., and the guard now runs on that normalized
    #    write_path (not the raw path), so the typo is still caught and nothing is mkdir'd.
    drive_prefix = str(drive_root).lstrip("/")
    sneaky = _write(f"{drive_prefix}/skills/external/sneaky/plugin.py", "code")
    assert "DATA_WRITE_ERROR" in sneaky and "skill payload not found" in sneaky, sneaky
    assert not (drive_root / "skills" / "external" / "sneaky").exists(), "normalized-prefix typo must not mkdir a payload"
