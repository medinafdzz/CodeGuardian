from codeguardian.models import Issue
from codeguardian.text import read_file_lines
from codeguardian.validation import filter_valid_issues, patched_file_content, validate_issue


def make_issue(file_path, **overrides):
    data = {
        "sonar_key": "S1",
        "file": str(file_path),
        "target_type": "function",
        "target_name": "calculate",
        "line": 2,
        "original_start_line": 2,
        "original_end_line": 2,
        "problem": "Unsafe value",
        "severity": "MAJOR",
        "solution": "Use a safe value",
        "original_code": "    value = 1",
        "proposed_code": "    value = 2",
    }
    data.update(overrides)
    return Issue(**data)


def test_patched_file_content_replaces_only_the_expected_line_range(tmp_path):
    source = tmp_path / "sample.py"
    source.write_text("def calculate():\n    value = 1\n    return value\n", encoding="utf-8")
    read_file_lines.cache_clear()

    patched = patched_file_content(make_issue(source))

    assert patched == "def calculate():\n    value = 2\n    return value\n"


def test_validate_issue_accepts_python_patch_when_syntax_remains_valid(tmp_path):
    source = tmp_path / "valid_patch.py"
    source.write_text("def calculate():\n    value = 1\n    return value\n", encoding="utf-8")
    read_file_lines.cache_clear()

    is_valid, reason = validate_issue(make_issue(source))

    assert is_valid is True
    assert reason == ""


def test_validate_issue_rejects_python_patch_when_syntax_is_invalid(tmp_path):
    source = tmp_path / "invalid_patch.py"
    source.write_text("def calculate():\n    value = 1\n    return value\n", encoding="utf-8")
    read_file_lines.cache_clear()

    is_valid, reason = validate_issue(make_issue(source, proposed_code="    value = "))

    assert is_valid is False
    assert "python syntax validation failed" in reason


def test_validate_issue_rejects_patch_when_original_code_does_not_match(tmp_path):
    source = tmp_path / "mismatch.py"
    source.write_text("def calculate():\n    value = 99\n    return value\n", encoding="utf-8")
    read_file_lines.cache_clear()

    is_valid, reason = validate_issue(make_issue(source))

    assert is_valid is False
    assert reason == "original_code does not match the current file content in the expected line range"


def test_filter_valid_issues_returns_valid_items_and_drop_count(tmp_path):
    valid_source = tmp_path / "valid.py"
    invalid_source = tmp_path / "invalid.py"
    valid_source.write_text("def calculate():\n    value = 1\n    return value\n", encoding="utf-8")
    invalid_source.write_text("def calculate():\n    value = 99\n    return value\n", encoding="utf-8")
    read_file_lines.cache_clear()

    valid_issues, dropped = filter_valid_issues([
        make_issue(valid_source, sonar_key="VALID"),
        make_issue(invalid_source, sonar_key="INVALID"),
    ])

    assert [issue.sonar_key for issue in valid_issues] == ["VALID"]
    assert dropped == 1
