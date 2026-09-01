import pytest

from codeguardian.text import detect_language
from codeguardian.models import ScopeInfo


@pytest.mark.parametrize(
    ("filepath", "expected_language"),
    [
        ("src/main/App.java", "java"),
        ("agent.py", "python"),
        ("web/app.js", "javascript"),
        ("web/app.ts", "typescript"),
        ("cmd/server.go", "go"),
        ("Services/Review.cs", "csharp"),
        ("native/analyzer.cpp", "cpp"),
        ("native/analyzer.cxx", "cpp"),
        ("native/analyzer.cc", "cpp"),
        ("native/analyzer.c", "c"),
        ("api/index.php", "php"),
        ("scripts/task.rb", "ruby"),
        ("src/lib.rs", "rust"),
        ("src/Main.kt", "kotlin"),
        ("ios/App.swift", "swift"),
        ("README.md", "unknown"),
    ],
)
def test_detect_language_from_file_extension(filepath, expected_language):
    assert detect_language(filepath) == expected_language


def test_detect_language_is_case_insensitive():
    assert detect_language("SRC/MAIN/SERVICE.JAVA") == "java"


def test_scope_info_can_be_constructed_with_scope_fields():
    scope = ScopeInfo("method", "calculate", 3, 8)

    assert scope.kind == "method"
    assert scope.name == "calculate"
    assert scope.start_line == 3
    assert scope.end_line == 8
