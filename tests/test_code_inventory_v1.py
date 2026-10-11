import json

from ouroboros.code_intelligence import build_code_inventory, render_codebase_digest


def test_code_inventory_indexes_python_symbols_imports_and_no_raw_source_cache(tmp_path):
    repo = tmp_path / "repo"
    data = tmp_path / "data"
    (repo / "pkg").mkdir(parents=True)
    (repo / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    (repo / "pkg" / "helper.py").write_text("VALUE = 1\n", encoding="utf-8")
    (repo / "pkg" / "main.py").write_text(
        "import pkg.helper\n\n"
        "from .helper import VALUE\n\n"
        "from . import helper\n\n"
        "CONST = 'INVENTORY_RAW_SOURCE_SENTINEL'\n\n"
        "class Worker:\n"
        "    pass\n\n"
        "async def run():\n"
        "    return CONST\n",
        encoding="utf-8",
    )

    inventory = build_code_inventory(repo, drive_root=data, persist=True)
    files = {file.path: file for file in inventory.files}
    main = files["pkg/main.py"]

    assert main.sha256
    assert main.language == "python"
    assert {symbol.name for symbol in main.symbols} >= {"Worker", "run", "CONST"}
    assert "pkg.helper" in main.imports
    assert "pkg/helper.py" in main.resolved_import_paths
    assert any(call.name == "CONST" for call in main.references)

    digest = render_codebase_digest(inventory)
    assert "pkg/main.py" in digest
    assert "Worker" in digest

    cache_files = list((data / "state" / "code_intel").glob("*/inventory.json"))
    assert len(cache_files) == 1
    cached = json.loads(cache_files[0].read_text(encoding="utf-8"))
    rendered_cache = json.dumps(cached)
    assert "INVENTORY_RAW_SOURCE_SENTINEL" not in rendered_cache
    assert "return CONST" not in rendered_cache
    assert cached["schema_version"] == 2


def test_code_inventory_classifies_sensitive_and_symlink_escape(tmp_path):
    repo = tmp_path / "repo"
    data = tmp_path / "data"
    outside = tmp_path / "outside.txt"
    repo.mkdir()
    outside.write_text("external", encoding="utf-8")
    (repo / ".env").write_text("OPENAI_API_KEY=thisisaverylongsecretvalue123456", encoding="utf-8")
    (repo / "app.py").write_text("x = 1\n", encoding="utf-8")
    (repo / "escape").symlink_to(outside)

    inventory = build_code_inventory(repo, drive_root=data, persist=True)
    files = {file.path: file for file in inventory.files}

    assert files[".env"].disposition == "sensitive"
    assert files[".env"].sha256 == ""
    assert files[".env"].size == 0
    assert files["escape"].disposition == "path_escape"

    cache_files = list((data / "state" / "code_intel").glob("*/inventory.json"))
    cached = json.loads(cache_files[0].read_text(encoding="utf-8"))
    rendered_cache = json.dumps(cached)
    assert "thisisaverylongsecretvalue" not in rendered_cache


def test_code_inventory_rebuilds_v1_cache(tmp_path):
    repo = tmp_path / "repo"
    data = tmp_path / "data"
    repo.mkdir()
    (repo / "app.py").write_text("def run():\n    return 1\n", encoding="utf-8")

    build_code_inventory(repo, drive_root=data, persist=True)
    cache_file = next((data / "state" / "code_intel").glob("*/inventory.json"))
    cached = json.loads(cache_file.read_text(encoding="utf-8"))
    cached["schema_version"] = 1
    for file in cached["files"]:
        file.pop("call_sites", None)
        file.pop("references", None)
    cache_file.write_text(json.dumps(cached), encoding="utf-8")

    rebuilt = build_code_inventory(repo, drive_root=data, persist=True)
    assert rebuilt.schema_version == 2
    rebuilt_cache = json.loads(cache_file.read_text(encoding="utf-8"))
    assert rebuilt_cache["schema_version"] == 2
    app = {file.path: file for file in rebuilt.files}["app.py"]
    assert hasattr(app, "call_sites")
