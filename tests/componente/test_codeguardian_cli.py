import json
import subprocess

from tools.codeguardian_cli import (
    apply_suggestions,
    filter_suggestions,
    load_results_file,
    suggestion_status,
    undo_suggestions,
    validate_local_match,
)


def suggestion(identifier, file_name, start, end, original, proposed):
    return {
        "id": identifier,
        "source": "sonarqube",
        "severity": "MAJOR",
        "file": file_name,
        "line": start,
        "original_start_line": start,
        "original_end_line": end,
        "target_name": "run",
        "problem": "Problem",
        "solution": "Solution",
        "original_code": original,
        "proposed_code": proposed,
        "required_imports": [],
    }


def test_cli_loads_and_filters_suggestions(tmp_path):
    results = {
        "suggestions": [
            suggestion("one", "src/app.py", 1, 1, "old", "new"),
            {**suggestion("two", "tests/app.py", 1, 1, "old", "new"), "severity": "MINOR"},
        ]
    }
    path = tmp_path / "codeguardian-results.json"
    path.write_text(json.dumps(results), encoding="utf-8")

    loaded = load_results_file(path)
    filtered = filter_suggestions(loaded["suggestions"], severity="MAJOR", path="src/")

    assert [item["id"] for item in filtered] == ["one"]


def test_cli_refuses_to_apply_when_original_code_does_not_match(tmp_path):
    source = tmp_path / "app.py"
    source.write_text("def run():\n    return changed\n", encoding="utf-8")
    item = suggestion("one", "app.py", 2, 2, "    return old", "    return new")

    ok, reason = validate_local_match(item, tmp_path)

    assert ok is False
    assert "original_code does not match" in reason


def test_cli_applies_valid_suggestion(tmp_path):
    source = tmp_path / "app.py"
    source.write_text("def run():\n    return old\n", encoding="utf-8")
    item = suggestion("one", "app.py", 2, 2, "    return old", "    return new")

    summary = apply_suggestions([item], tmp_path)

    assert summary["applied"] == 1
    assert source.read_text(encoding="utf-8") == "def run():\n    return new\n"


def test_cli_applies_python_required_import(tmp_path):
    source = tmp_path / "app.py"
    source.write_text('"""Module docstring."""\n\n\ndef run(value):\n    return value\n', encoding="utf-8")
    item = {
        **suggestion("one", "app.py", 4, 5, "def run(value):\n    return value", "def run(value):\n    return sqrt(value)"),
        "required_imports": ["from math import sqrt"],
    }

    summary = apply_suggestions([item], tmp_path)

    assert summary["applied"] == 1
    assert source.read_text(encoding="utf-8") == (
        '"""Module docstring."""\n'
        "\n"
        "\n"
        "from math import sqrt\n"
        "\n"
        "def run(value):\n"
        "    return sqrt(value)\n"
    )


def test_cli_applies_java_required_import_after_package(tmp_path):
    source = tmp_path / "App.java"
    source.write_text(
        "package demo;\n"
        "\n"
        "class App {\n"
        "    Object run() {\n"
        "        return null;\n"
        "    }\n"
        "}\n",
        encoding="utf-8",
    )
    item = {
        **suggestion(
            "one",
            "App.java",
            4,
            6,
            "    Object run() {\n        return null;\n    }",
            "    Object run() {\n        return Optional.empty();\n    }",
        ),
        "required_imports": ["import java.util.Optional;"],
    }

    summary = apply_suggestions([item], tmp_path)

    assert summary["applied"] == 1
    assert source.read_text(encoding="utf-8") == (
        "package demo;\n"
        "\n"
        "import java.util.Optional;\n"
        "\n"
        "class App {\n"
        "    Object run() {\n"
        "        return Optional.empty();\n"
        "    }\n"
        "}\n"
    )


def test_cli_rolls_back_java_change_when_build_validation_fails(tmp_path, monkeypatch):
    source = tmp_path / "App.java"
    original = (
        "class App {\n"
        "    String run() {\n"
        "        return \"old\";\n"
        "    }\n"
        "}\n"
    )
    source.write_text(original, encoding="utf-8")
    (tmp_path / "pom.xml").write_text("<project />", encoding="utf-8")
    item = suggestion(
        "one",
        "App.java",
        2,
        4,
        "    String run() {\n        return \"old\";\n    }",
        "    String run() {\n        return \"new\";\n    }",
    )

    def fake_run(*_args, **_kwargs):
        return subprocess.CompletedProcess(args=["mvn"], returncode=1, stdout="", stderr="compile failed")

    monkeypatch.setattr("tools.codeguardian_cli.subprocess.run", fake_run)

    summary = apply_suggestions([item], tmp_path)

    assert summary["applied"] == 0
    assert "java build validation failed" in summary["results"][0]["message"]
    assert source.read_text(encoding="utf-8") == original


def test_cli_rejects_invalid_required_import(tmp_path):
    source = tmp_path / "app.py"
    source.write_text("def run():\n    return old\n", encoding="utf-8")
    item = {
        **suggestion("one", "app.py", 2, 2, "    return old", "    return new"),
        "required_imports": ["not an import"],
    }

    summary = apply_suggestions([item], tmp_path)

    assert summary["applied"] == 0
    assert "invalid python import" in summary["results"][0]["message"]
    assert source.read_text(encoding="utf-8") == "def run():\n    return old\n"


def test_cli_does_not_write_python_syntax_error(tmp_path):
    source = tmp_path / "app.py"
    source.write_text("def run():\n    return old\n", encoding="utf-8")
    item = suggestion("one", "app.py", 2, 2, "    return old", "    value = ")

    summary = apply_suggestions([item], tmp_path)

    assert summary["applied"] == 0
    assert "python syntax validation failed" in summary["results"][0]["message"]
    assert source.read_text(encoding="utf-8") == "def run():\n    return old\n"


def test_cli_applies_same_file_suggestions_bottom_to_top(tmp_path):
    source = tmp_path / "app.py"
    source.write_text("a\nb\nc\n", encoding="utf-8")
    first = suggestion("first", "app.py", 1, 1, "a", "aa")
    third = suggestion("third", "app.py", 3, 3, "c", "cc")

    summary = apply_suggestions([first, third], tmp_path)

    assert summary["applied"] == 2
    assert source.read_text(encoding="utf-8") == "aa\nb\ncc\n"


def test_cli_rebases_proposed_code_to_current_indent(tmp_path):
    source = tmp_path / "App.java"
    source.write_text(
        "class App {\n"
        "    void run() {\n"
        "    }\n"
        "}\n",
        encoding="utf-8",
    )
    item = suggestion(
        "one",
        "App.java",
        2,
        3,
        "void run() {\n    }",
        "void run() {\n        throw new UnsupportedOperationException();\n    }",
    )

    summary = apply_suggestions([item], tmp_path)

    assert summary["applied"] == 1
    assert source.read_text(encoding="utf-8") == (
        "class App {\n"
        "    void run() {\n"
        "        throw new UnsupportedOperationException();\n"
        "    }\n"
        "}\n"
    )


def test_cli_accepts_indentation_differences_on_all_lines(tmp_path):
    source = tmp_path / "App.java"
    source.write_text(
        "class App {\n"
        "        void run() {\n"
        "            call();\n"
        "        }\n"
        "}\n",
        encoding="utf-8",
    )
    item = suggestion(
        "one",
        "App.java",
        2,
        4,
        "void run() {\n    call();\n}",
        "void run() {\n    safeCall();\n}",
    )

    summary = apply_suggestions([item], tmp_path)

    assert summary["applied"] == 1
    assert source.read_text(encoding="utf-8") == (
        "class App {\n"
        "        void run() {\n"
        "            safeCall();\n"
        "        }\n"
        "}\n"
    )


def test_cli_finds_original_block_after_line_shift(tmp_path):
    source = tmp_path / "app.py"
    source.write_text("inserted\nheader\nold\nfooter\n", encoding="utf-8")
    item = suggestion("one", "app.py", 2, 2, "old", "new")

    summary = apply_suggestions([item], tmp_path)

    assert summary["applied"] == 1
    assert source.read_text(encoding="utf-8") == "inserted\nheader\nnew\nfooter\n"


def test_cli_treats_existing_proposed_code_as_already_applied(tmp_path):
    source = tmp_path / "app.py"
    source.write_text("def run():\n    return new\n", encoding="utf-8")
    item = suggestion("one", "app.py", 2, 2, "    return old", "    return new")

    summary = apply_suggestions([item], tmp_path)

    assert summary["applied"] == 1
    assert summary["results"][0]["message"] == "already applied"
    assert source.read_text(encoding="utf-8") == "def run():\n    return new\n"


def test_cli_reports_status_by_suggestion_block(tmp_path):
    source = tmp_path / "app.py"
    item = suggestion("one", "app.py", 2, 2, "    return old", "    return new")

    source.write_text("def run():\n    return old\n", encoding="utf-8")
    assert suggestion_status(item, tmp_path) == "open"

    source.write_text("def run():\n    return new\n", encoding="utf-8")
    assert suggestion_status(item, tmp_path) == "applied"

    source.write_text("def run():\n    return custom\n", encoding="utf-8")
    assert suggestion_status(item, tmp_path) == "changed"


def test_cli_undo_restores_original_code(tmp_path):
    source = tmp_path / "app.py"
    source.write_text("def run():\n    return new\n", encoding="utf-8")
    item = suggestion("one", "app.py", 2, 2, "    return old", "    return new")

    summary = undo_suggestions([item], tmp_path)

    assert summary["applied"] == 1
    assert summary["results"][0]["message"] == "undone"
    assert source.read_text(encoding="utf-8") == "def run():\n    return old\n"
    assert suggestion_status(item, tmp_path) == "open"
