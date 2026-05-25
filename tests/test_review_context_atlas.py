from __future__ import annotations

from pathlib import Path

from ouroboros.tools.review_context_atlas import (
    ReviewContextAtlasRequest,
    compile_review_context_atlas,
)


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _coverage(pack):
    return {row["path"]: row for row in pack.manifest["coverage"]}


def test_atlas_accounts_for_every_tracked_path_and_excludes_unrelated_tests(tmp_path):
    _write(tmp_path / "app.py", "import helper\n\ndef run():\n    return helper.value()\n")
    _write(tmp_path / "helper.py", "def value():\n    return 42\n")
    _write(tmp_path / "pkg" / "__init__.py", "")
    _write(tmp_path / "pkg" / "main.py", "from .helper import thing\n\nanswer = thing()\n")
    _write(tmp_path / "pkg" / "helper.py", "def thing():\n    return 7\n")
    _write(tmp_path / "tests" / "test_app.py", "def test_app():\n    assert True\n")
    _write(tmp_path / "docs" / "CHECKLISTS.md", "canonical checklist\n")

    tracked = (
        "app.py",
        "helper.py",
        "pkg/__init__.py",
        "pkg/main.py",
        "pkg/helper.py",
        "tests/test_app.py",
        "docs/CHECKLISTS.md",
    )
    pack = compile_review_context_atlas(
        ReviewContextAtlasRequest(
            repo_dir=tmp_path,
            tracked_paths=tracked,
            anchors=("app.py",),
            already_included=frozenset({"docs/CHECKLISTS.md"}),
            fixed_prompt_tokens=100,
            target_total_tokens=20_000,
            hard_total_tokens=25_000,
            include_tests=False,
        )
    )

    coverage = _coverage(pack)
    assert set(coverage) == set(tracked)
    assert coverage["docs/CHECKLISTS.md"]["disposition"] == "already_included"
    assert coverage["tests/test_app.py"]["disposition"] == "excluded_test"
    assert "pkg.helper" in coverage["pkg/main.py"]["imports"]
    assert "def test_app" not in pack.text


def test_atlas_include_tests_allows_test_files(tmp_path):
    _write(tmp_path / "tests" / "test_app.py", "def test_app():\n    assert True\n")

    pack = compile_review_context_atlas(
        ReviewContextAtlasRequest(
            repo_dir=tmp_path,
            tracked_paths=("tests/test_app.py",),
            anchors=("tests/test_app.py",),
            fixed_prompt_tokens=100,
            target_total_tokens=20_000,
            hard_total_tokens=25_000,
            include_tests=True,
        )
    )

    coverage = _coverage(pack)
    assert coverage["tests/test_app.py"]["disposition"] == "full"
    assert "def test_app" in pack.text


def test_atlas_force_includes_protected_workflow_even_under_skipped_github_dir(tmp_path):
    _write(tmp_path / ".github" / "workflows" / "ci.yml", "name: CI\n")
    _write(tmp_path / "ouroboros" / "tools" / "review_context_atlas.py", "ATLAS = True\n")
    _write(tmp_path / "assets" / "logo.txt", "asset text\n")
    _write(tmp_path / "main.py", "print('main')\n")

    pack = compile_review_context_atlas(
        ReviewContextAtlasRequest(
            repo_dir=tmp_path,
            tracked_paths=(
                ".github/workflows/ci.yml",
                "ouroboros/tools/review_context_atlas.py",
                "assets/logo.txt",
                "main.py",
            ),
            fixed_prompt_tokens=100,
            target_total_tokens=20_000,
            hard_total_tokens=25_000,
        )
    )

    coverage = _coverage(pack)
    assert coverage[".github/workflows/ci.yml"]["disposition"] == "full"
    assert coverage["ouroboros/tools/review_context_atlas.py"]["disposition"] == "full"
    assert "name: CI" in pack.text
    assert coverage["assets/logo.txt"]["disposition"] == "excluded_dir"
    assert "asset text" not in pack.text


def test_atlas_marks_sensitive_binary_oversized_and_vendored_files(tmp_path):
    _write(tmp_path / ".env.example", "TOKEN=secret\n")
    (tmp_path / "image.png").write_bytes(b"\x89PNG\r\n\x00")
    _write(tmp_path / "script.min.js", "minified();\n")
    (tmp_path / "huge.py").write_bytes(b"x" * (1_048_576 + 1))
    normal_source = "\n".join(f"import pkg_{idx}" for idx in range(30))
    normal_source += '\nDATABASE_URL = "postgres://alice:secretpw@db.local/app"\n'
    normal_source += "\n".join(f"def f_{idx}():\n    return {idx}\n" for idx in range(20))
    _write(tmp_path / "normal.py", normal_source)

    pack = compile_review_context_atlas(
        ReviewContextAtlasRequest(
            repo_dir=tmp_path,
            tracked_paths=(".env.example", "image.png", "script.min.js", "huge.py", "normal.py"),
            fixed_prompt_tokens=100,
            target_total_tokens=20_000,
            hard_total_tokens=25_000,
        )
    )

    coverage = _coverage(pack)
    assert coverage[".env.example"]["disposition"] == "sensitive"
    assert coverage[".env.example"]["sha256"] == ""
    assert coverage[".env.example"]["size"] == 0
    assert coverage["image.png"]["disposition"] == "binary_media"
    assert coverage["script.min.js"]["disposition"] == "vendored_minified"
    assert coverage["huge.py"]["disposition"] == "oversized"
    assert coverage["normal.py"]["disposition"] == "full"
    assert coverage["normal.py"]["imports_total"] == 30
    assert coverage["normal.py"]["symbols_total"] >= 20
    assert len(coverage["normal.py"]["imports"]) <= 12
    assert "secretpw" not in pack.text
    assert "postgres://***REDACTED***@db.local/app" in pack.text


def test_atlas_respects_total_prompt_target_and_reports_budget_manifest_only(tmp_path):
    tracked = []
    for idx in range(8):
        rel = f"pkg/mod_{idx}.py"
        tracked.append(rel)
        _write(tmp_path / rel, ("def f():\n    return 'x'\n" * 120))

    pack = compile_review_context_atlas(
        ReviewContextAtlasRequest(
            repo_dir=tmp_path,
            tracked_paths=tuple(tracked),
            fixed_prompt_tokens=100,
            target_total_tokens=5_000,
            hard_total_tokens=8_000,
        )
    )

    assert pack.manifest["estimated_total_tokens"] <= 8_000
    assert pack.manifest["selected_count"] < len(tracked)
    assert any(row["disposition"] == "manifest_only" for row in pack.manifest["coverage"])
    assert pack.status in {"budget_constrained", "ok"}

    _write(tmp_path / "BIBLE.md", "constitution\n" * 500)
    overflow = compile_review_context_atlas(
        ReviewContextAtlasRequest(
            repo_dir=tmp_path,
            tracked_paths=("BIBLE.md",),
            fixed_prompt_tokens=100,
            target_total_tokens=300,
            hard_total_tokens=350,
        )
    )
    assert overflow.status == "budget_exceeded"
    assert _coverage(overflow)["BIBLE.md"]["disposition"] == "budget_omitted"
