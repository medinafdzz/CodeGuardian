from dataclasses import replace

from codeguardian.models import Issue, PerformanceCandidate
from codeguardian.performance import (
    build_performance_prompt,
    collect_performance_candidates,
    demo_fast_mode,
    optimization_batch_size,
    performance_batch_signature,
    performance_enabled,
    performance_max_scopes,
    performance_issue_key,
    has_required_performance_metadata,
)
from codeguardian.validation import normalize_issues


def test_performance_review_is_disabled_by_default(monkeypatch):
    monkeypatch.delenv("CODEGUARDIAN_ENABLE_OPTIMIZATION_REVIEW", raising=False)
    monkeypatch.delenv("CODEGUARDIAN_ENABLE_PERFORMANCE_REVIEW", raising=False)

    assert performance_enabled() is False


def test_performance_review_can_be_enabled(monkeypatch):
    monkeypatch.setenv("CODEGUARDIAN_ENABLE_OPTIMIZATION_REVIEW", "true")

    assert performance_enabled() is True


def test_performance_review_keeps_legacy_env_compatibility(monkeypatch):
    monkeypatch.delenv("CODEGUARDIAN_ENABLE_OPTIMIZATION_REVIEW", raising=False)
    monkeypatch.setenv("CODEGUARDIAN_ENABLE_PERFORMANCE_REVIEW", "true")

    assert performance_enabled() is True


def test_demo_fast_mode_sets_aggressive_defaults(monkeypatch):
    monkeypatch.setenv("CODEGUARDIAN_DEMO_FAST_MODE", "true")
    monkeypatch.delenv("CODEGUARDIAN_OPTIMIZATION_MAX_SCOPES", raising=False)
    monkeypatch.delenv("CODEGUARDIAN_MAX_OPTIMIZATION_SCOPES", raising=False)
    monkeypatch.delenv("CODEGUARDIAN_OPTIMIZATION_BATCH_SIZE", raising=False)

    assert demo_fast_mode() is True
    assert performance_max_scopes() == 5
    assert optimization_batch_size() == 3


def test_max_optimization_scopes_alias_is_supported(monkeypatch):
    monkeypatch.delenv("CODEGUARDIAN_OPTIMIZATION_MAX_SCOPES", raising=False)
    monkeypatch.setenv("CODEGUARDIAN_MAX_OPTIMIZATION_SCOPES", "4")

    assert performance_max_scopes() == 4


def test_collect_performance_candidates_uses_changed_function_scopes(monkeypatch, tmp_path):
    source = tmp_path / "service.py"
    source.write_text(
        "def unchanged():\n"
        "    return []\n\n"
        "def find_matches(users, ids):\n"
        "    result = []\n"
        "    for user in users:\n"
        "        if user.id in ids:\n"
        "            result.append(user)\n"
        "    return result\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("codeguardian.performance.diff_base_ref", lambda: "origin/main...HEAD")
    monkeypatch.setattr("codeguardian.performance.changed_files", lambda base_ref, max_files: ["service.py"])

    def fake_run_git(args):
        assert args == ["diff", "--unified=0", "origin/main...HEAD", "--", "service.py"]
        return "@@ -5,0 +7,1 @@\n+        if user.id in ids:\n"

    monkeypatch.setattr("codeguardian.performance.run_git", fake_run_git)

    candidates = collect_performance_candidates(max_scopes=10)

    assert len(candidates) == 1
    assert candidates[0].file == "service.py"
    assert candidates[0].target_type == "function"
    assert candidates[0].target_name == "find_matches"
    assert candidates[0].start_line == 4
    assert "def find_matches" in candidates[0].code


def test_performance_signature_does_not_collide_with_sonar_signature():
    candidate = PerformanceCandidate(
        file="service.py",
        target_type="function",
        target_name="find_matches",
        start_line=4,
        end_line=9,
        language="python",
        code="def find_matches(users, ids):\n    return []",
    )

    signature = performance_batch_signature("demo", candidate)

    assert signature != "demo"
    assert signature == performance_batch_signature("demo", candidate)
    assert signature != performance_batch_signature("demo", replace(candidate, code="def x():\n    pass"))


def test_performance_issue_key_is_stable_and_internal():
    candidate = PerformanceCandidate(
        file="service.py",
        target_type="function",
        target_name="find_matches",
        start_line=4,
        end_line=9,
        language="python",
        code="def find_matches(users, ids):\n    return []",
    )

    key = performance_issue_key(candidate)

    assert key.startswith("OPTIMIZATION:")
    assert key == performance_issue_key(candidate)


def test_performance_prompt_requires_complexity_fields():
    candidate = PerformanceCandidate(
        file="service.py",
        target_type="function",
        target_name="find_matches",
        start_line=4,
        end_line=9,
        language="python",
        code="def find_matches(users, ids):\n    return []",
    )

    prompt = build_performance_prompt("demo", candidate)

    assert "Estimate both time complexity and space complexity" in prompt
    assert "OPTIMIZATION CANDIDATE" in prompt
    assert "Do not assume a fixed pattern" in prompt


def test_collect_performance_candidates_includes_changed_build_files(monkeypatch, tmp_path):
    source = tmp_path / "Jenkinsfile"
    source.write_text(
        "pipeline {\n"
        "  stages {\n"
        "    stage('Build') { steps { sh 'mvn clean install' } }\n"
        "  }\n"
        "}\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("codeguardian.performance.diff_base_ref", lambda: "origin/main...HEAD")
    monkeypatch.setattr("codeguardian.performance.changed_files", lambda base_ref, max_files: ["Jenkinsfile"])
    monkeypatch.setattr(
        "codeguardian.performance.run_git",
        lambda args: "@@ -3,0 +3,1 @@\n+    stage('Build') { steps { sh 'mvn clean install' } }\n",
    )

    candidates = collect_performance_candidates(max_scopes=10)

    assert len(candidates) == 1
    assert candidates[0].file == "Jenkinsfile"
    assert candidates[0].target_type == "file"
    assert candidates[0].target_name == "Jenkinsfile"
    assert candidates[0].language == "groovy"


def test_fast_mode_skips_config_files_for_optimization(monkeypatch, tmp_path):
    source = tmp_path / "Jenkinsfile"
    source.write_text("pipeline { stages { stage('Build') { steps { sh 'mvn test' } } } }\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("CODEGUARDIAN_DEMO_FAST_MODE", "true")
    monkeypatch.setattr("codeguardian.performance.diff_base_ref", lambda: "origin/main...HEAD")
    monkeypatch.setattr("codeguardian.performance.changed_files", lambda base_ref, max_files: ["Jenkinsfile"])

    candidates = collect_performance_candidates(max_scopes=10)

    assert candidates == []


def test_performance_issue_normalization_preserves_complexity_fields():
    issue = Issue(
        sonar_key="OPTIMIZATION:abc",
        source="optimization",
        file="service.py",
        target_type="function",
        target_name="find_matches",
        line=4,
        original_start_line=4,
        original_end_line=5,
        problem="Nested lookup performs repeated linear scans.",
        severity="OPTIMIZATION",
        solution="Use a set for membership lookup.",
        original_complexity="Time: O(n*m), Space: O(1)",
        proposed_complexity="Time: O(n+m), Space: O(m)",
        complexity_justification="A hash set avoids repeated scans.",
        original_code="def find_matches(users, ids):\n    return []",
        proposed_code="def find_matches(users, ids):\n    return list(users)",
    )

    normalized, dropped = normalize_issues([issue])

    assert dropped == 0
    assert normalized[0].source == "optimization"
    assert normalized[0].original_complexity == "Time: O(n*m), Space: O(1)"
    assert normalized[0].proposed_complexity == "Time: O(n+m), Space: O(m)"


def test_performance_metadata_is_required():
    issue = Issue(
        sonar_key="OPTIMIZATION:abc",
        source="optimization",
        file="service.py",
        target_type="function",
        target_name="find_matches",
        line=4,
        original_start_line=4,
        original_end_line=5,
        problem="Repeated membership checks scan the list for every item.",
        severity="OPTIMIZATION",
        solution="Use a set for membership lookup.",
        original_complexity="O(n*m)",
        proposed_complexity="O(n+m)",
        complexity_justification="A hash set avoids repeated scans.",
        original_code="def find_matches(users, ids):\n    return []",
        proposed_code="def find_matches(users, ids):\n    return list(users)",
    )

    assert has_required_performance_metadata(issue) is True
    issue.complexity_justification = ""
    assert has_required_performance_metadata(issue) is False
