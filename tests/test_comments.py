from agent import (
    CODEGUARDIAN_AGENT_MARKER,
    Issue,
    comment_content,
    extract_issue_key,
    hidden_ids,
    is_agent_comment,
    wrap_agent_comment,
)


def make_issue(**overrides):
    data = {
        "sonar_key": "S1",
        "file": "src/service.py",
        "target_type": "function",
        "target_name": "calculate",
        "line": 2,
        "original_start_line": 2,
        "original_end_line": 2,
        "problem": "Problem description",
        "severity": "MAJOR",
        "solution": "Solution description",
        "original_code": "value = 1",
        "proposed_code": "value = 2",
        "required_imports": [],
    }
    data.update(overrides)
    return Issue(**data)


def test_hidden_ids_deduplicates_keys_and_extract_issue_key_reads_them_back():
    metadata = hidden_ids(["S1", "S2", "S1", " ", ""])

    assert metadata == "<!-- CodeGuardian-IDs:\nID: S1\nID: S2\n-->"
    assert set(extract_issue_key(metadata)) == {"S1", "S2"}


def test_extract_issue_key_supports_legacy_comma_separated_metadata():
    comment = "<!-- CodeGuardian-IDs: OLD-1, OLD-2 -->"

    assert set(extract_issue_key(comment)) == {"OLD-1", "OLD-2"}


def test_wrap_agent_comment_adds_marker_used_to_identify_agent_comments():
    comment = wrap_agent_comment("body")

    assert comment == f"{CODEGUARDIAN_AGENT_MARKER}\nbody"
    assert is_agent_comment(comment) is True
    assert is_agent_comment("human comment") is False


def test_comment_content_contains_single_issue_review_blocks_and_metadata():
    content = comment_content([make_issue(required_imports=["import os"])])

    assert content.startswith(CODEGUARDIAN_AGENT_MARKER)
    assert "### Code Issue" in content
    assert "**File:** src/service.py" in content
    assert "**Lines:** 2-2" in content
    assert "**Severity:** MAJOR" in content
    assert "**Additional required imports:**" in content
    assert "import os" in content
    assert "ID: S1" in content


def test_comment_content_combines_multiple_issues_for_same_block():
    content = comment_content([
        make_issue(sonar_key="S1", line=2, problem="First problem", solution="Shared solution"),
        make_issue(sonar_key="S2", line=3, problem="Second problem", solution="Shared solution"),
    ])

    assert "### Code Issues" in content
    assert "- Line 2 (MAJOR): First problem" in content
    assert "- Line 3 (MAJOR): Second problem" in content
    assert "**Suggested solution:**" in content
    assert "ID: S1" in content
    assert "ID: S2" in content
