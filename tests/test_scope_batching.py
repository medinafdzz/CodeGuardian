from agent import build_scope_batches


def test_build_scope_batches_groups_issues_from_same_function_scope():
    issues = [
        {
            "sonar_key": "S2",
            "file": "src/service.py",
            "line": 12,
            "scope_kind": "function",
            "scope_name": "calculate",
            "scope_start_line": 10,
            "scope_end_line": 20,
        },
        {
            "sonar_key": "S1",
            "file": "src/service.py",
            "line": 11,
            "scope_kind": "function",
            "scope_name": "calculate",
            "scope_start_line": 10,
            "scope_end_line": 20,
        },
    ]

    batches = build_scope_batches(issues)

    assert len(batches) == 1
    assert [issue["sonar_key"] for issue in batches[0]] == ["S1", "S2"]


def test_build_scope_batches_keeps_global_issues_separated():
    issues = [
        {
            "sonar_key": "S1",
            "file": "src/config.py",
            "line": 1,
            "scope_kind": "global",
            "scope_name": "",
            "scope_start_line": 1,
            "scope_end_line": 1,
        },
        {
            "sonar_key": "S2",
            "file": "src/config.py",
            "line": 2,
            "scope_kind": "global",
            "scope_name": "",
            "scope_start_line": 2,
            "scope_end_line": 2,
        },
    ]

    batches = build_scope_batches(issues)

    assert len(batches) == 2
    assert [batch[0]["sonar_key"] for batch in batches] == ["S1", "S2"]
