from codeguardian.improvements import changed_files, improvements_enabled


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
