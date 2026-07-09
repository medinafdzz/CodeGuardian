import subprocess

import pytest

from codeguardian.models import AgentExecutionError
from codeguardian.validation import validate_maven_compile


def test_validate_maven_compile_skips_when_pom_is_missing(tmp_path, monkeypatch):
    calls = []

    def fake_run(*args, **kwargs):
        calls.append((args, kwargs))

    monkeypatch.setattr(subprocess, "run", fake_run)

    result = validate_maven_compile(tmp_path)

    assert result.executed is False
    assert result.success is True
    assert result.reason == "pom.xml not found"
    assert calls == []


def test_validate_maven_compile_runs_maven_command_when_pom_exists(tmp_path, monkeypatch):
    (tmp_path / "pom.xml").write_text("<project></project>", encoding="utf-8")
    calls = []

    def fake_run(*args, **kwargs):
        calls.append((args, kwargs))
        return subprocess.CompletedProcess(args[0], 0, stdout="ok", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    result = validate_maven_compile(tmp_path)

    assert result.executed is True
    assert result.success is True
    assert result.reason == ""
    assert calls[0][0][0] == ["mvn", "-B", "-q", "-ntp", "-DskipTests", "compile"]
    assert calls[0][1]["cwd"] == tmp_path
    assert calls[0][1]["text"] is True


def test_validate_maven_compile_raises_when_compilation_fails(tmp_path, monkeypatch):
    (tmp_path / "pom.xml").write_text("<project></project>", encoding="utf-8")

    def fake_run(*args, **kwargs):
        return subprocess.CompletedProcess(args[0], 1, stdout="compile output", stderr="compile error")

    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(AgentExecutionError, match="Maven compile validation failed"):
        validate_maven_compile(tmp_path)
