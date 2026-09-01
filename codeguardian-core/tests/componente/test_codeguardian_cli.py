import json

import pytest

import tools.codeguardian_cli as cli_module
from tools.codeguardian_cli import (
    apply_suggestions,
    build_parser,
    filter_suggestions,
    load_results_file,
    suggestion_status,
    suggestion_statuses,
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


def test_cli_applies_java_change_without_running_local_build(tmp_path):
    source = tmp_path / "App.java"
    source.write_text(
        "class App {\n"
        "    String run() {\n"
        "        return \"old\";\n"
        "    }\n"
        "}\n",
        encoding="utf-8",
    )
    (tmp_path / "pom.xml").write_text("<project />", encoding="utf-8")
    item = suggestion(
        "one",
        "App.java",
        2,
        4,
        "    String run() {\n        return \"old\";\n    }",
        "    String run() {\n        return \"new\";\n    }",
    )

    summary = apply_suggestions([item], tmp_path)

    assert summary["applied"] == 1
    assert 'return "new";' in source.read_text(encoding="utf-8")


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
    source.write_text("def run():\n    return old\n", encoding="utf-8")
    item = suggestion("one", "app.py", 2, 2, "    return old", "    return new")

    applied = apply_suggestions([item], tmp_path)
    summary = undo_suggestions([item], tmp_path)

    assert applied["applied"] == 1
    assert summary["applied"] == 1
    assert summary["results"][0]["message"] == "undone"
    assert summary["results"][0]["restored"] is True
    assert source.read_text(encoding="utf-8") == "def run():\n    return old\n"
    assert suggestion_status(item, tmp_path) == "open"


def test_cli_rejects_ambiguous_original_code_after_line_shift(tmp_path):
    source = tmp_path / "app.py"
    source.write_text("same\nother\nsame\n", encoding="utf-8")
    item = suggestion("one", "app.py", 2, 2, "same", "changed")

    ok, reason = validate_local_match(item, tmp_path)
    summary = apply_suggestions([item], tmp_path)

    assert not ok
    assert "ambiguous" in reason
    assert summary["applied"] == 0
    assert source.read_text(encoding="utf-8") == "same\nother\nsame\n"


def test_cli_preserves_lf_line_endings_on_windows(tmp_path):
    source = tmp_path / "app.py"
    source.write_bytes(b"old\nnext\n")
    item = suggestion("one", "app.py", 1, 1, "old", "new")

    summary = apply_suggestions([item], tmp_path)

    assert summary["applied"] == 1
    assert source.read_bytes() == b"new\nnext\n"


def test_cli_applies_suggestion_in_path_with_spaces(tmp_path):
    source = tmp_path / "folder with spaces" / "app.py"
    source.parent.mkdir()
    source.write_text("old\n", encoding="utf-8")
    item = suggestion("one", "folder with spaces/app.py", 1, 1, "old", "new")

    summary = apply_suggestions([item], tmp_path)

    assert summary["applied"] == 1
    assert source.read_text(encoding="utf-8") == "new\n"


def test_cli_applies_auxiliary_edit_with_required_import(tmp_path):
    source = tmp_path / "app.py"
    source.write_text("def helper():\n    return old\n\ndef run():\n    return old\n", encoding="utf-8")
    item = suggestion("one", "app.py", 4, 5, "def run():\n    return old", "def run():\n    return Path('new')")
    item["required_imports"] = ["from pathlib import Path"]
    item["auxiliary_edits"] = [{
        "original_code": "def helper():\n    return old",
        "proposed_code": "def helper():\n    return 'updated'",
    }]

    summary = apply_suggestions([item], tmp_path)

    assert summary["applied"] == 1
    content = source.read_text(encoding="utf-8")
    assert "from pathlib import Path" in content
    assert "return 'updated'" in content
    assert "return Path('new')" in content


def test_cli_status_reads_each_file_once(tmp_path, monkeypatch):
    source = tmp_path / "app.py"
    source.write_text("one\ntwo\n", encoding="utf-8")
    items = [
        suggestion("one", "app.py", 1, 1, "one", "first"),
        suggestion("two", "app.py", 2, 2, "two", "second"),
    ]
    original_read = cli_module._read_text
    reads = []

    def counted_read(path):
        reads.append(path)
        return original_read(path)

    monkeypatch.setattr(cli_module, "_read_text", counted_read)

    statuses = suggestion_statuses(items, tmp_path)

    assert [item["status"] for item in statuses] == ["open", "open"]
    assert reads == [source]


def test_cli_apply_commands_accept_machine_readable_output():
    parser = build_parser()

    single = parser.parse_args(["apply", "--file", "results.json", "--id", "one", "--json"])
    selected = parser.parse_args(["apply-selected", "--file", "results.json", "--ids", "one,two", "--json"])
    undo = parser.parse_args(["undo", "--file", "results.json", "--id", "one", "--json"])
    undo_selected = parser.parse_args([
        "undo-selected", "--file", "results.json", "--ids", "one,two", "--json",
    ])
    state_clean = parser.parse_args(["state-clean", "--older-than-days", "7", "--json"])

    assert single.json is True
    assert selected.json is True
    assert undo.json is True
    assert undo_selected.json is True
    assert state_clean.older_than_days == 7
    assert state_clean.json is True


def test_cli_undo_restores_python_import_and_auxiliary_edits(tmp_path):
    source = tmp_path / "app.py"
    original = "def helper():\n    return old\n\ndef run():\n    return old\n"
    source.write_text(original, encoding="utf-8")
    item = suggestion("one", "app.py", 4, 5, "def run():\n    return old", "def run():\n    return Path('new')")
    item["required_imports"] = ["from pathlib import Path"]
    item["auxiliary_edits"] = [{
        "original_code": "def helper():\n    return old",
        "proposed_code": "def helper():\n    return 'updated'",
    }]

    assert apply_suggestions([item], tmp_path)["applied"] == 1
    state = json.loads(cli_module._undo_state_path(tmp_path).read_text(encoding="utf-8"))
    assert state["transactions"][0]["imports_added"] == ["from pathlib import Path"]
    assert len(state["transactions"][0]["auxiliary_edits"]) == 1
    summary = undo_suggestions([item], tmp_path)

    assert summary["applied"] == 1
    assert source.read_text(encoding="utf-8") == original


def test_cli_undo_restores_java_import(tmp_path):
    source = tmp_path / "App.java"
    original = (
        "package demo;\n\n"
        "class App {\n"
        "    Object run() {\n"
        "        return null;\n"
        "    }\n"
        "}\n"
    )
    source.write_text(original, encoding="utf-8")
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

    assert apply_suggestions([item], tmp_path)["applied"] == 1
    assert "import java.util.Optional;" in source.read_text(encoding="utf-8")
    assert undo_suggestions([item], tmp_path)["applied"] == 1
    assert source.read_text(encoding="utf-8") == original


def test_cli_undo_restores_removed_import(tmp_path):
    source = tmp_path / "app.py"
    original = "import unused\n\ndef run():\n    return old\n"
    source.write_text(original, encoding="utf-8")
    item = suggestion("one", "app.py", 3, 4, "def run():\n    return old", "def run():\n    return new")
    item["optional_removed_imports"] = ["import unused"]

    assert apply_suggestions([item], tmp_path)["applied"] == 1
    assert "import unused" not in source.read_text(encoding="utf-8")
    state = json.loads(cli_module._undo_state_path(tmp_path).read_text(encoding="utf-8"))
    assert state["transactions"][0]["imports_removed"] == ["import unused"]
    assert undo_suggestions([item], tmp_path)["applied"] == 1
    assert source.read_text(encoding="utf-8") == original


@pytest.mark.parametrize("newline", [b"\n", b"\r\n"])
def test_cli_transaction_preserves_exact_line_endings(tmp_path, newline):
    source = tmp_path / "app.py"
    original = newline.join([b"old", b"next", b""])
    source.write_bytes(original)
    item = suggestion("one", "app.py", 1, 1, "old", "new")

    assert apply_suggestions([item], tmp_path)["applied"] == 1
    state = json.loads(cli_module._undo_state_path(tmp_path).read_text(encoding="utf-8"))
    assert state["transactions"][0]["imports_added"] == []
    assert undo_suggestions([item], tmp_path)["applied"] == 1
    assert source.read_bytes() == original


def test_cli_transaction_handles_paths_with_spaces(tmp_path):
    source = tmp_path / "folder with spaces" / "app.py"
    source.parent.mkdir()
    source.write_text("old\n", encoding="utf-8")
    item = suggestion("one", "folder with spaces/app.py", 1, 1, "old", "new")

    assert apply_suggestions([item], tmp_path)["applied"] == 1
    assert undo_suggestions([item], tmp_path)["applied"] == 1
    assert source.read_text(encoding="utf-8") == "old\n"


def test_cli_undo_keeps_import_that_existed_before_apply(tmp_path):
    source = tmp_path / "app.py"
    original = "from pathlib import Path\n\ndef run():\n    return 'old'\n"
    source.write_text(original, encoding="utf-8")
    item = suggestion("one", "app.py", 3, 4, "def run():\n    return 'old'", "def run():\n    return Path('new')")
    item["required_imports"] = ["from pathlib import Path"]

    assert apply_suggestions([item], tmp_path)["applied"] == 1
    assert undo_suggestions([item], tmp_path)["applied"] == 1
    assert source.read_text(encoding="utf-8") == original


def test_cli_repeated_apply_does_not_duplicate_transaction(tmp_path):
    source = tmp_path / "app.py"
    source.write_text("old\n", encoding="utf-8")
    item = suggestion("one", "app.py", 1, 1, "old", "new")

    first = apply_suggestions([item], tmp_path)
    second = apply_suggestions([item], tmp_path)
    state = json.loads(cli_module._undo_state_path(tmp_path).read_text(encoding="utf-8"))

    assert first["applied"] == 1
    assert second["applied"] == 1
    assert second["results"][0]["message"] == "already applied"
    assert len(state["transactions"]) == 1


def test_cli_reapplies_after_manual_revert_to_original_content(tmp_path):
    source = tmp_path / "app.py"
    original = "def run():\n    return old\n"
    source.write_text(original, encoding="utf-8")
    item = suggestion("one", "app.py", 2, 2, "    return old", "    return new")

    first = apply_suggestions([item], tmp_path)
    source.write_text(original, encoding="utf-8")
    second = apply_suggestions([item], tmp_path)

    assert first["applied"] == 1
    assert second["applied"] == 1
    assert second["results"][0]["message"] == "applied"
    assert source.read_text(encoding="utf-8") == "def run():\n    return new\n"

    state = json.loads(cli_module._undo_state_path(tmp_path).read_text(encoding="utf-8"))
    assert [transaction["status"] for transaction in state["transactions"]] == [
        "manually_reverted",
        "committed",
    ]
    assert state["transactions"][0]["transaction_id"] != state["transactions"][1]["transaction_id"]

    undone = undo_suggestions([item], tmp_path)

    assert undone["applied"] == 1
    assert source.read_text(encoding="utf-8") == original

    cleanup = cli_module.cleanup_undo_state(tmp_path, older_than_days=0)
    cleaned_state = json.loads(cli_module._undo_state_path(tmp_path).read_text(encoding="utf-8"))
    assert cleanup["removed"] == 2
    assert cleaned_state["transactions"] == []


def test_cli_reapplies_imports_and_auxiliary_edits_after_manual_revert(tmp_path):
    source = tmp_path / "app.py"
    original = "def helper():\n    return old\n\ndef run():\n    return old\n"
    source.write_text(original, encoding="utf-8")
    item = suggestion("one", "app.py", 4, 5, "def run():\n    return old", "def run():\n    return Path('new')")
    item["required_imports"] = ["from pathlib import Path"]
    item["auxiliary_edits"] = [{
        "original_code": "def helper():\n    return old",
        "proposed_code": "def helper():\n    return 'updated'",
    }]

    assert apply_suggestions([item], tmp_path)["applied"] == 1
    source.write_text(original, encoding="utf-8")

    reapplied = apply_suggestions([item], tmp_path)

    assert reapplied["applied"] == 1
    content = source.read_text(encoding="utf-8")
    assert "from pathlib import Path" in content
    assert "return 'updated'" in content
    assert "return Path('new')" in content

    assert undo_suggestions([item], tmp_path)["applied"] == 1
    assert source.read_text(encoding="utf-8") == original


def test_cli_reapply_stays_blocked_for_intermediate_content(tmp_path):
    source = tmp_path / "app.py"
    source.write_text("old\n", encoding="utf-8")
    item = suggestion("one", "app.py", 1, 1, "old", "new")
    assert apply_suggestions([item], tmp_path)["applied"] == 1
    source.write_text("custom\n", encoding="utf-8")

    summary = apply_suggestions([item], tmp_path)

    assert summary["applied"] == 0
    assert "file has changed" in summary["results"][0]["blocked_reason"]
    assert source.read_text(encoding="utf-8") == "custom\n"


def test_cli_recovers_pending_transaction_after_source_write(tmp_path):
    source = tmp_path / "app.py"
    source.write_text("old\n", encoding="utf-8")
    item = suggestion("one", "app.py", 1, 1, "old", "new")
    assert apply_suggestions([item], tmp_path)["applied"] == 1

    state_path = cli_module._undo_state_path(tmp_path)
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state["transactions"][0]["status"] = "pending"
    state["transactions"][0].pop("committed_at", None)
    state_path.write_text(json.dumps(state), encoding="utf-8")

    recovered = apply_suggestions([item], tmp_path)
    recovered_state = json.loads(state_path.read_text(encoding="utf-8"))

    assert recovered["applied"] == 1
    assert recovered["results"][0]["message"] == "already applied"
    assert recovered_state["transactions"][0]["status"] == "committed"
    assert recovered_state["transactions"][0]["recovered_after_interruption"] is True
    assert len(recovered_state["transactions"]) == 1
    assert source.read_text(encoding="utf-8") == "new\n"


def test_cli_repeated_undo_is_idempotent(tmp_path):
    source = tmp_path / "app.py"
    source.write_text("old\n", encoding="utf-8")
    item = suggestion("one", "app.py", 1, 1, "old", "new")
    apply_suggestions([item], tmp_path)

    first = undo_suggestions([item], tmp_path)
    second = undo_suggestions([item], tmp_path)

    assert first["applied"] == 1
    assert second["applied"] == 1
    assert second["results"][0]["message"] == "already open"
    assert source.read_text(encoding="utf-8") == "old\n"


def test_cli_undo_rejects_file_changed_after_apply(tmp_path):
    source = tmp_path / "app.py"
    source.write_text("old\n", encoding="utf-8")
    item = suggestion("one", "app.py", 1, 1, "old", "new")
    apply_suggestions([item], tmp_path)
    source.write_text("new\nuser change\n", encoding="utf-8")

    summary = undo_suggestions([item], tmp_path)

    assert summary["applied"] == 0
    assert "file has changed" in summary["results"][0]["blocked_reason"]
    assert source.read_text(encoding="utf-8") == "new\nuser change\n"


def test_cli_undo_selected_reverses_transactions_on_same_file(tmp_path):
    source = tmp_path / "app.py"
    source.write_text("a\nb\n", encoding="utf-8")
    first = suggestion("first", "app.py", 1, 1, "a", "aa")
    second = suggestion("second", "app.py", 2, 2, "b", "bb")

    assert apply_suggestions([first, second], tmp_path)["applied"] == 2
    summary = undo_suggestions([second, first], tmp_path)

    assert summary["applied"] == 2
    assert [result["id"] for result in summary["results"]] == ["first", "second"]
    assert source.read_text(encoding="utf-8") == "a\nb\n"


def test_cli_rejects_undo_below_later_transaction(tmp_path):
    source = tmp_path / "app.py"
    source.write_text("a\nb\n", encoding="utf-8")
    first = suggestion("first", "app.py", 1, 1, "a", "aa")
    second = suggestion("second", "app.py", 2, 2, "b", "bb")
    apply_suggestions([first, second], tmp_path)

    summary = undo_suggestions([second], tmp_path)

    assert summary["applied"] == 0
    assert "later CodeGuardian change" in summary["results"][0]["blocked_reason"]
    assert source.read_text(encoding="utf-8") == "aa\nbb\n"


def test_cli_undo_restores_java_change_without_running_local_build(tmp_path):
    source = tmp_path / "App.java"
    original = "class App {\n    String value() { return \"old\"; }\n}\n"
    source.write_text(original, encoding="utf-8")
    (tmp_path / "pom.xml").write_text("<project />", encoding="utf-8")
    item = suggestion(
        "one",
        "App.java",
        2,
        2,
        '    String value() { return "old"; }',
        '    String value() { return "new"; }',
    )
    assert apply_suggestions([item], tmp_path)["applied"] == 1

    summary = undo_suggestions([item], tmp_path)

    assert summary["applied"] == 1
    assert source.read_text(encoding="utf-8") == original


def test_cli_undo_rejects_missing_transaction_state(tmp_path):
    source = tmp_path / "app.py"
    source.write_text("old\n", encoding="utf-8")
    item = suggestion("one", "app.py", 1, 1, "old", "new")
    apply_suggestions([item], tmp_path)
    cli_module._undo_state_path(tmp_path).unlink()

    summary = undo_suggestions([item], tmp_path)

    assert summary["applied"] == 0
    assert "no committed transaction" in summary["results"][0]["blocked_reason"]
    assert source.read_text(encoding="utf-8") == "new\n"


def test_cli_rejects_corrupt_transaction_state_without_writing(tmp_path):
    source = tmp_path / "app.py"
    source.write_text("old\n", encoding="utf-8")
    state_path = cli_module._undo_state_path(tmp_path)
    state_path.parent.mkdir()
    state_path.write_text("not json", encoding="utf-8")
    item = suggestion("one", "app.py", 1, 1, "old", "new")

    summary = apply_suggestions([item], tmp_path)

    assert summary["failed"] == 1
    assert "corrupt" in summary["results"][0]["message"]
    assert source.read_text(encoding="utf-8") == "old\n"


def test_cli_machine_output_includes_transaction_details(tmp_path, monkeypatch, capsys):
    source = tmp_path / "app.py"
    source.write_text("old\n", encoding="utf-8")
    artifact = tmp_path / "codeguardian-results.json"
    artifact.write_text(json.dumps({
        "run_id": "build-1",
        "head_commit": "abc123",
        "suggestions": [suggestion("one", "app.py", 1, 1, "old", "new")],
    }), encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    assert cli_module.main(["apply", "--file", str(artifact), "--id", "one", "--json"]) == 0
    result = json.loads(capsys.readouterr().out)

    assert result["results"][0]["transaction_id"]
    assert result["results"][0]["before_hash"]
    assert result["results"][0]["after_hash"]
    state = json.loads(cli_module._undo_state_path(tmp_path).read_text(encoding="utf-8"))
    assert state["transactions"][0]["artifact"]["run_id"] == "build-1"
    assert state["transactions"][0]["artifact"]["head_commit"] == "abc123"


def test_cli_excludes_local_state_from_git(tmp_path):
    source = tmp_path / "app.py"
    source.write_text("old\n", encoding="utf-8")
    exclude = tmp_path / ".git" / "info" / "exclude"
    exclude.parent.mkdir(parents=True)
    exclude.write_text("# local excludes\n", encoding="utf-8")
    item = suggestion("one", "app.py", 1, 1, "old", "new")

    apply_suggestions([item], tmp_path)

    assert "/.codeguardian/" in exclude.read_text(encoding="utf-8").splitlines()


def test_cli_source_write_failure_leaves_no_partial_change(tmp_path, monkeypatch):
    source = tmp_path / "app.py"
    source.write_text("old\n", encoding="utf-8")
    item = suggestion("one", "app.py", 1, 1, "old", "new")
    original_write = cli_module._atomic_write_bytes

    def failing_write(path, content, mode=None):
        if path == source:
            raise OSError("simulated source write failure")
        return original_write(path, content, mode)

    monkeypatch.setattr(cli_module, "_atomic_write_bytes", failing_write)

    summary = apply_suggestions([item], tmp_path)
    state = json.loads(cli_module._undo_state_path(tmp_path).read_text(encoding="utf-8"))

    assert summary["failed"] == 1
    assert source.read_text(encoding="utf-8") == "old\n"
    assert state["transactions"][0]["status"] == "rolled_back"


def test_cli_rejects_path_outside_repository(tmp_path):
    outside = tmp_path.parent / "outside.py"
    outside.write_text("old\n", encoding="utf-8")
    item = suggestion("one", str(outside), 1, 1, "old", "new")

    summary = apply_suggestions([item], tmp_path)

    assert summary["applied"] == 0
    assert "escapes the repository" in summary["results"][0]["message"]
    assert outside.read_text(encoding="utf-8") == "old\n"


def test_cli_state_cleanup_removes_only_completed_transactions(tmp_path):
    source = tmp_path / "app.py"
    source.write_text("old\n", encoding="utf-8")
    item = suggestion("one", "app.py", 1, 1, "old", "new")
    apply_suggestions([item], tmp_path)
    undo_suggestions([item], tmp_path)

    result = cli_module.cleanup_undo_state(tmp_path, older_than_days=0)

    assert result["removed"] == 1
    state = json.loads(cli_module._undo_state_path(tmp_path).read_text(encoding="utf-8"))
    assert state["transactions"] == []
