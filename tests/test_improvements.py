from codeguardian.improvements import (
    align_issue_to_current_file,
    build_improvement_prompt,
    detect_cpp_improvement_candidates,
    changed_files,
    detect_improvement_candidates,
    detect_java_improvement_candidates,
    detect_python_improvement_candidates,
    detect_shell_improvement_candidates,
    improvements_enabled,
)
from codeguardian.models import ImprovementCandidate, Issue


def test_improvements_enabled_accepts_true_values(monkeypatch):
    monkeypatch.setenv("CODEGUARDIAN_ENABLE_IMPROVEMENTS", "true")

    assert improvements_enabled() is True


def test_improvements_enabled_is_disabled_by_default(monkeypatch):
    monkeypatch.delenv("CODEGUARDIAN_ENABLE_IMPROVEMENTS", raising=False)

    assert improvements_enabled() is False


def test_changed_files_filters_generated_and_missing_paths(monkeypatch, tmp_path):
    (tmp_path / "service.py").write_text("print('ok')\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    def fake_run_git(args):
        assert args == ["diff", "--name-only", "--diff-filter=AM", "origin/main...HEAD"]
        return "\n".join([
            "service.py",
            "node_modules/generated.js",
            "missing.py",
        ])

    monkeypatch.setattr("codeguardian.improvements.run_git", fake_run_git)

    assert changed_files("origin/main...HEAD", max_files=5) == ["service.py"]


def test_changed_files_uses_configurable_exclusions(monkeypatch, tmp_path):
    for path in [
        "service.py",
        "generated/client.py",
        "essFramework/release/runtime.py",
        "models/service_pb2.py",
    ]:
        file_path = tmp_path / path
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text("print('ok')\n", encoding="utf-8")

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv(
        "CODEGUARDIAN_IMPROVEMENT_EXCLUDE",
        "generated/,essFramework/release/,*_pb2.py",
    )

    def fake_run_git(args):
        assert args == ["diff", "--name-only", "--diff-filter=AM", "origin/main...HEAD"]
        return "\n".join([
            "service.py",
            "generated/client.py",
            "essFramework/release/runtime.py",
            "models/service_pb2.py",
        ])

    monkeypatch.setattr("codeguardian.improvements.run_git", fake_run_git)

    assert changed_files("origin/main...HEAD", max_files=5) == ["service.py"]


def test_improvement_candidate_keeps_detection_evidence():
    candidate = ImprovementCandidate(
        file="service.py",
        line=12,
        language="python",
        category="complexity",
        reason="Function has too many nested branches.",
        evidence="nesting_depth=4",
        original_code="def run():\n    if enabled:\n        return True",
        confidence=0.8,
    )

    assert candidate.file == "service.py"
    assert candidate.line == 12
    assert candidate.language == "python"
    assert candidate.category == "complexity"
    assert candidate.evidence == "nesting_depth=4"
    assert candidate.confidence == 0.8


def test_detect_python_improvement_candidates_reports_long_functions(tmp_path):
    source = tmp_path / "service.py"
    source.write_text(
        "def compact():\n"
        "    return 1\n\n"
        "def too_long():\n"
        "    value = 1\n"
        "    value += 1\n"
        "    value += 1\n"
        "    value += 1\n"
        "    return value\n",
        encoding="utf-8",
    )

    candidates = detect_python_improvement_candidates(str(source), max_function_lines=4)

    assert len(candidates) == 1
    assert candidates[0].file == str(source)
    assert candidates[0].line == 4
    assert candidates[0].language == "python"
    assert candidates[0].category == "complexity"
    assert "function_lines=6" in candidates[0].evidence
    assert "def too_long" in candidates[0].original_code


def test_detect_python_improvement_candidates_reports_broad_exceptions(tmp_path):
    source = tmp_path / "service.py"
    source.write_text(
        "def load():\n"
        "    try:\n"
        "        return int('x')\n"
        "    except Exception as error:\n"
        "        return None\n",
        encoding="utf-8",
    )

    candidates = detect_python_improvement_candidates(str(source), max_function_lines=20)

    assert len(candidates) == 1
    assert candidates[0].line == 4
    assert candidates[0].category == "error_handling"
    assert "broad_exception=Exception" in candidates[0].evidence
    assert "except Exception" in candidates[0].original_code


def test_detect_shell_improvement_candidates_reports_command_substitution_iteration(tmp_path):
    source = tmp_path / "startup.sh"
    source.write_text(
        "#!/bin/bash\n"
        "for file in $(ls $ROOT/*.xml)\n"
        "do\n"
        "  echo $file\n"
        "done\n",
        encoding="utf-8",
    )

    candidates = detect_shell_improvement_candidates(str(source))

    assert len(candidates) == 1
    assert candidates[0].file == str(source)
    assert candidates[0].line == 2
    assert candidates[0].language == "shell"
    assert candidates[0].category == "maintainability"
    assert "command_substitution_iteration" in candidates[0].evidence
    assert "for file in $(ls" in candidates[0].original_code


def test_detect_shell_improvement_candidates_reports_unquoted_test_variables(tmp_path):
    source = tmp_path / "startup.ksh"
    source.write_text(
        "#!/bin/ksh\n"
        "if [ -d $ESS_HOME ] ; then\n"
        "  echo ready\n"
        "fi\n",
        encoding="utf-8",
    )

    candidates = detect_shell_improvement_candidates(str(source))

    assert len(candidates) == 1
    assert candidates[0].line == 2
    assert candidates[0].category == "resource_handling"
    assert "unquoted_test_variable=ESS_HOME" in candidates[0].evidence
    assert "[ -d $ESS_HOME ]" in candidates[0].original_code


def test_detect_java_improvement_candidates_reports_broad_catches(tmp_path):
    source = tmp_path / "Service.java"
    source.write_text(
        "class Service {\n"
        "  int load(String value) {\n"
        "    try {\n"
        "      return Integer.parseInt(value);\n"
        "    } catch (Exception error) {\n"
        "      return 0;\n"
        "    }\n"
        "  }\n"
        "}\n",
        encoding="utf-8",
    )

    candidates = detect_java_improvement_candidates(str(source))

    assert len(candidates) == 1
    assert candidates[0].line == 5
    assert candidates[0].language == "java"
    assert candidates[0].category == "error_handling"
    assert "broad_exception=Exception" in candidates[0].evidence
    assert "catch (Exception error)" in candidates[0].original_code


def test_detect_java_improvement_candidates_reports_console_prints(tmp_path):
    source = tmp_path / "Service.java"
    source.write_text(
        "class Service {\n"
        "  void run() {\n"
        "    System.out.println(\"ready\");\n"
        "  }\n"
        "}\n",
        encoding="utf-8",
    )

    candidates = detect_java_improvement_candidates(str(source))

    assert len(candidates) == 1
    assert candidates[0].line == 3
    assert candidates[0].language == "java"
    assert candidates[0].category == "observability"
    assert "console_print=System.out.println" in candidates[0].evidence
    assert "System.out.println" in candidates[0].original_code


def test_detect_cpp_improvement_candidates_reports_namespace_pollution(tmp_path):
    source = tmp_path / "service.cpp"
    source.write_text(
        "#include <vector>\n"
        "using namespace std;\n"
        "int main() { return 0; }\n",
        encoding="utf-8",
    )

    candidates = detect_cpp_improvement_candidates(str(source))

    assert len(candidates) == 1
    assert candidates[0].line == 2
    assert candidates[0].language == "cpp"
    assert candidates[0].category == "maintainability"
    assert "using_namespace_std" in candidates[0].evidence
    assert "using namespace std" in candidates[0].original_code


def test_detect_cpp_improvement_candidates_reports_manual_allocation(tmp_path):
    source = tmp_path / "service.cc"
    source.write_text(
        "void run() {\n"
        "  auto value = new Widget();\n"
        "  value->start();\n"
        "}\n",
        encoding="utf-8",
    )

    candidates = detect_cpp_improvement_candidates(str(source))

    assert len(candidates) == 1
    assert candidates[0].line == 2
    assert candidates[0].language == "cpp"
    assert candidates[0].category == "resource_management"
    assert "manual_new_allocation" in candidates[0].evidence
    assert "new Widget" in candidates[0].original_code


def test_detect_improvement_candidates_dispatches_supported_files(tmp_path):
    python_source = tmp_path / "service.py"
    python_source.write_text(
        "def load():\n"
        "    try:\n"
        "        return int('x')\n"
        "    except Exception:\n"
        "        return None\n",
        encoding="utf-8",
    )
    shell_source = tmp_path / "startup.ksh"
    shell_source.write_text(
        "#!/bin/ksh\n"
        "if [ -d $ESS_HOME ] ; then\n"
        "  echo ready\n"
        "fi\n",
        encoding="utf-8",
    )
    java_source = tmp_path / "Service.java"
    java_source.write_text(
        "class Service {\n"
        "  void run() {\n"
        "    System.out.println(\"ready\");\n"
        "  }\n"
        "}\n",
        encoding="utf-8",
    )
    cpp_source = tmp_path / "service.cpp"
    cpp_source.write_text(
        "#include <vector>\n"
        "using namespace std;\n",
        encoding="utf-8",
    )
    ignored_source = tmp_path / "README.md"
    ignored_source.write_text("# docs\n", encoding="utf-8")

    candidates = detect_improvement_candidates([
        str(python_source),
        str(shell_source),
        str(java_source),
        str(cpp_source),
        str(ignored_source),
    ])

    assert [candidate.language for candidate in candidates] == ["python", "shell", "java", "cpp"]
    assert [candidate.category for candidate in candidates] == [
        "error_handling",
        "resource_handling",
        "observability",
        "maintainability",
    ]


def test_build_improvement_prompt_includes_detected_candidates():
    candidate = ImprovementCandidate(
        file="service.py",
        line=10,
        language="python",
        category="complexity",
        reason="Function is long enough to make maintenance and testing harder.",
        evidence="function_lines=80;threshold=60",
        original_code="def service():\n    return True",
        confidence=0.7,
    )

    prompt = build_improvement_prompt(
        project_key="demo",
        max_improvements=3,
        files=["service.py"],
        diff_payload="diff --git a/service.py b/service.py",
        candidates=[candidate],
    )

    assert "Detected improvement candidates:" in prompt
    assert "function_lines=80;threshold=60" in prompt
    assert "Only publish suggestions that are supported by these candidates." in prompt
    assert "Diff context:" in prompt


def test_align_issue_to_current_file_updates_imprecise_line(monkeypatch, tmp_path):
    source = tmp_path / "service.py"
    source.write_text("def first():\n    return 1\n\n\ndef second():\n    return 2\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    issue = Issue(
        sonar_key="IMPROVEMENT:1",
        file="service.py",
        target_type="function",
        target_name="second",
        line=1,
        original_start_line=1,
        original_end_line=2,
        problem="The line is imprecise.",
        severity="IMPROVEMENT",
        solution="Use the exact block location.",
        original_code="def second():\n    return 2",
        proposed_code="def second():\n    value = 2\n    return value",
    )

    aligned = align_issue_to_current_file(issue)

    assert aligned.line == 5
    assert aligned.original_start_line == 5
    assert aligned.original_end_line == 6
