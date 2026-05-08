from agent import Issue, group_key, issue_key, normalize_issues


def make_issue(**overrides):
    data = {
        "sonar_key": "S1",
        "file": " src/main/App.java ",
        "target_type": " method ",
        "target_name": " calculate ",
        "line": 10,
        "problem": "Problem - detail",
        "severity": "major",
        "solution": "Solution - detail",
        "original_code": "`int value = 1;`",
        "proposed_code": "`int value = 2;`",
    }
    data.update(overrides)
    return Issue(**data)


def test_normalize_issues_cleans_text_and_line_range():
    issue = make_issue(line=0, original_start_line=8, original_end_line=3)

    normalized, dropped = normalize_issues([issue])

    assert dropped == 0
    assert len(normalized) == 1
    result = normalized[0]
    assert result.file == "src/main/App.java"
    assert result.target_type == "method"
    assert result.target_name == "calculate"
    assert result.line == 1
    assert result.original_start_line == 3
    assert result.original_end_line == 8
    assert result.problem == "Problem\n- detail"
    assert result.solution == "Solution\n- detail"
    assert result.severity == "MAJOR"
    assert result.original_code == "int value = 1;"
    assert result.proposed_code == "int value = 2;"


def test_normalize_issues_drops_empty_or_identical_replacements():
    identical = make_issue(original_code="return value;", proposed_code="return value;")
    empty_original = make_issue(sonar_key="S2", original_code="", proposed_code="return value;")

    normalized, dropped = normalize_issues([identical, empty_original])

    assert normalized == []
    assert dropped == 2


def test_normalize_issues_deduplicates_real_sonar_keys():
    first = make_issue(sonar_key="DUPLICATED")
    second = make_issue(sonar_key="DUPLICATED", line=20)

    normalized, dropped = normalize_issues([first, second])

    assert dropped == 0
    assert len(normalized) == 1


def test_issue_key_falls_back_when_sonar_key_is_missing():
    issue = make_issue(sonar_key="NO_KEY", file="service.py", line=7, target_name="run", severity="CRITICAL")

    assert issue_key(issue) == "service.py:7:run:CRITICAL"


def test_group_key_uses_normalized_original_and_proposed_code():
    issue = make_issue(
        original_code="value = 1   \n",
        proposed_code="value = 2   \n",
    )

    assert group_key(issue) == (" src/main/App.java ", "value = 1", "value = 2")
