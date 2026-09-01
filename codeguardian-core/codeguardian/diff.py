import fnmatch
import os
import subprocess


DEFAULT_DIFF_EXCLUSIONS = (
    ".git/",
    "node_modules/",
    "target/",
    "build/",
    ".venv/",
    "venv/",
    ".codeguardian-venv/",
    "__pycache__/",
    ".ruff_cache/",
)


def run_git(args: list[str]) -> str:
    result = subprocess.run(
        ["git", *args],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.returncode != 0:
        return ""
    return result.stdout.strip()


def diff_exclusions() -> tuple[str, ...]:
    configured = os.getenv("CODEGUARDIAN_DIFF_EXCLUDE", "")
    extra_patterns = [
        pattern.strip().replace("\\", "/")
        for chunk in configured.replace(";", ",").split(",")
        for pattern in chunk.splitlines()
        if pattern.strip()
    ]
    return (*DEFAULT_DIFF_EXCLUSIONS, *extra_patterns)


def is_diff_path_excluded(path: str, exclusions: tuple[str, ...] | None = None) -> bool:
    normalized_path = path.replace("\\", "/").lstrip("./")
    patterns = exclusions or diff_exclusions()

    for pattern in patterns:
        normalized_pattern = pattern.replace("\\", "/").lstrip("./")
        if normalized_pattern.endswith("/") and normalized_path.startswith(normalized_pattern):
            return True
        if fnmatch.fnmatch(normalized_path, normalized_pattern):
            return True

    return False


def diff_base_ref() -> str:
    target = os.getenv("CHANGE_TARGET", "main").strip() or "main"
    candidates = [
        f"origin/{target}...HEAD",
        f"{target}...HEAD",
        "HEAD~1...HEAD",
    ]

    for candidate in candidates:
        if run_git(["diff", "--name-only", candidate]):
            return candidate

    return ""


def changed_files(base_ref: str, max_files: int) -> list[str]:
    if not base_ref:
        return []

    output = run_git(["diff", "--name-only", "--diff-filter=AM", base_ref])
    files = []
    exclusions = diff_exclusions()

    for raw_path in output.splitlines():
        path = raw_path.strip()
        if not path or is_diff_path_excluded(path, exclusions):
            continue
        if not os.path.isfile(path):
            continue
        files.append(path)
        if len(files) >= max_files:
            break

    return files
