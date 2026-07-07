#!/usr/bin/env python3
import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from codeguardian.patching import apply_additional_file_edits, required_edits_present, validate_patched_text


def load_results_file(path: str | Path) -> dict[str, Any]:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def filter_suggestions(
    suggestions: list[dict[str, Any]],
    severity: str = "",
    source: str = "",
    path: str = "",
    current_file: str = "",
    limit: int = 0,
) -> list[dict[str, Any]]:
    result = suggestions
    if severity:
        result = [item for item in result if str(item.get("severity", "")).upper() == severity.upper()]
    if source:
        result = [item for item in result if str(item.get("source", "")).lower() == source.lower()]
    if path:
        result = [item for item in result if str(item.get("file", "")).startswith(path)]
    if current_file:
        normalized = current_file.replace("\\", "/")
        result = [item for item in result if str(item.get("file", "")).replace("\\", "/") == normalized]
    if limit > 0:
        result = result[:limit]
    return result


def find_suggestion(results: dict[str, Any], suggestion_id: str) -> dict[str, Any]:
    for suggestion in results.get("suggestions", []):
        if str(suggestion.get("id")) == str(suggestion_id):
            return suggestion
    raise KeyError(f"Suggestion not found: {suggestion_id}")


def _repo_path(file_path: str, root: Path | None = None) -> Path:
    path = Path(file_path)
    if path.is_absolute():
        return path
    return (root or Path.cwd()) / path


def _find_build_root(path: Path, root: Path | None = None) -> Path | None:
    root = (root or Path.cwd()).resolve()
    current = path.resolve().parent
    while True:
        if (current / "pom.xml").is_file() or (current / "build.gradle").is_file() or (current / "build.gradle.kts").is_file():
            return current
        if current == root or current.parent == current:
            return None
        current = current.parent


def _java_build_command(build_root: Path) -> list[str] | None:
    if (build_root / "pom.xml").is_file():
        return ["mvn", "-q", "-DskipTests", "compile"]
    if (build_root / "gradlew.bat").is_file():
        return [str(build_root / "gradlew.bat"), "compileJava", "-q"]
    if (build_root / "gradlew").is_file():
        return [str(build_root / "gradlew"), "compileJava", "-q"]
    if (build_root / "build.gradle").is_file() or (build_root / "build.gradle.kts").is_file():
        return ["gradle", "compileJava", "-q"]
    return None


def _validate_java_build_if_available(path: Path, root: Path | None = None) -> tuple[bool, str]:
    if path.suffix.lower() != ".java":
        return True, ""
    build_root = _find_build_root(path, root)
    if build_root is None:
        return True, ""
    command = _java_build_command(build_root)
    if command is None:
        return True, ""
    try:
        result = subprocess.run(
            command,
            cwd=build_root,
            capture_output=True,
            text=True,
            timeout=180,
        )
    except FileNotFoundError:
        return False, f"build command not found: {command[0]}"
    except subprocess.TimeoutExpired:
        return False, "java build validation timed out"
    if result.returncode != 0:
        output = "\n".join(part for part in [result.stdout, result.stderr] if part).strip()
        return False, "java build validation failed" + (f": {output[-1000:]}" if output else "")
    return True, ""


def _line_ending(text: str) -> str:
    return "\r\n" if "\r\n" in text else "\n"


def _with_original_trailing_newline(proposed: str, original_block: str) -> str:
    if original_block.endswith(("\r\n", "\n")) and not proposed.endswith(("\r\n", "\n")):
        return proposed + _line_ending(original_block)
    return proposed


def _first_line_indent(text: str) -> str:
    first_line = text.splitlines()[0] if text.splitlines() else ""
    return first_line[:len(first_line) - len(first_line.lstrip())]


def _leading_indent_agnostic_match(current: str, expected: str) -> bool:
    current_lines = current.rstrip("\r\n").splitlines()
    expected_lines = expected.rstrip("\r\n").splitlines()
    if not current_lines or len(current_lines) != len(expected_lines):
        return False

    return all(current_line.lstrip() == expected_line.lstrip()
               for current_line, expected_line in zip(current_lines, expected_lines))


def _normalized_block_lines(text: str) -> list[str]:
    return [line.lstrip().rstrip() for line in text.rstrip("\r\n").splitlines()]


def _first_non_empty_indent(text: str) -> str:
    for line in text.splitlines():
        if line.strip():
            return line[:len(line) - len(line.lstrip())]
    return ""


def _rebase_proposed_indent(proposed: str, expected: str, current: str) -> str:
    expected_base = _first_non_empty_indent(expected)
    current_base = _first_non_empty_indent(current)
    if expected_base == current_base:
        return proposed

    lines = proposed.splitlines(keepends=True)
    rebased = []
    for line in lines:
        if not line.strip():
            rebased.append(line)
        elif expected_base and line.startswith(expected_base):
            rebased.append(current_base + line[len(expected_base):])
        elif current_base and not line.startswith(current_base):
            rebased.append(current_base + line)
        else:
            rebased.append(line)
    return "".join(rebased)


def _line_range(suggestion: dict[str, Any]) -> tuple[int, int]:
    start = int(suggestion.get("original_start_line") or suggestion.get("line") or 0)
    end = int(suggestion.get("original_end_line") or suggestion.get("line") or 0)
    return start, end


def _blocks_match(current: str, expected: str) -> bool:
    return (
        current.rstrip("\r\n") == expected.rstrip("\r\n")
        or _leading_indent_agnostic_match(current, expected)
    )


def _find_matching_range(lines: list[str], expected: str, preferred_start: int) -> tuple[int, int, str] | None:
    expected_lines = expected.rstrip("\r\n").splitlines()
    if not expected_lines:
        return None

    window_size = len(expected_lines)
    expected_normalized = _normalized_block_lines(expected)
    matches: list[tuple[int, int, str]] = []
    for index in range(0, len(lines) - window_size + 1):
        current = "".join(lines[index:index + window_size])
        if _normalized_block_lines(current) == expected_normalized:
            matches.append((index + 1, index + window_size, current))

    if not matches:
        return None

    matches.sort(key=lambda item: abs(item[0] - preferred_start))
    return matches[0]


def _locate_block(
    suggestion: dict[str, Any],
    block: str,
    root: Path | None = None,
) -> tuple[Path, int, int, str, str] | None:
    path = _repo_path(str(suggestion.get("file", "")), root)
    if not path.is_file():
        return None

    start, end = _line_range(suggestion)
    if start < 1 or end < start:
        return None

    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    if end <= len(lines):
        current = "".join(lines[start - 1:end])
        if _blocks_match(current, block):
            return path, start, end, current, block

    found = _find_matching_range(lines, block, start)
    if found:
        found_start, found_end, current = found
        return path, found_start, found_end, current, block

    return None


def _locate_original_block(suggestion: dict[str, Any], root: Path | None = None) -> tuple[Path, int, int, str, str] | None:
    return _locate_block(suggestion, str(suggestion.get("original_code") or ""), root)


def _locate_proposed_block(suggestion: dict[str, Any], root: Path | None = None) -> tuple[Path, int, int, str, str] | None:
    return _locate_block(suggestion, str(suggestion.get("proposed_code") or ""), root)


def validate_local_match(suggestion: dict[str, Any], root: Path | None = None) -> tuple[bool, str]:
    path = _repo_path(str(suggestion.get("file", "")), root)
    if not path.is_file():
        return False, f"File not found: {path}"

    start, end = _line_range(suggestion)
    if start < 1 or end < start:
        return False, "Invalid original line range"

    proposed = _locate_proposed_block(suggestion, root)
    if _locate_original_block(suggestion, root) is None and proposed is None:
        return False, "original_code does not match the current file content"

    if proposed is not None and not required_edits_present(path.read_text(encoding="utf-8"), suggestion, path):
        return False, "required imports or auxiliary edits are missing"

    return True, ""


def apply_suggestion(suggestion: dict[str, Any], root: Path | None = None) -> tuple[bool, str]:
    ok, reason = validate_local_match(suggestion, root)
    if not ok:
        return False, reason

    target_path = _repo_path(str(suggestion.get("file", "")), root)
    located = _locate_original_block(suggestion, root)
    if located is None:
        proposed = _locate_proposed_block(suggestion, root)
        if proposed is not None and required_edits_present(target_path.read_text(encoding="utf-8"), suggestion, target_path):
            return True, "already applied"
        if proposed is not None:
            return False, "proposed_code is present but required imports or auxiliary edits are missing"
        return False, "original_code does not match the current file content"

    path, start, end, original_block, expected = located
    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    proposed = _rebase_proposed_indent(str(suggestion.get("proposed_code") or ""), expected, original_block)
    proposed = _with_original_trailing_newline(proposed, original_block)
    new_content = "".join(lines[:start - 1]) + proposed + "".join(lines[end:])
    ok, new_content, reason = apply_additional_file_edits(new_content, suggestion, path)
    if not ok:
        return False, reason
    ok, reason = validate_patched_text(new_content, path)
    if not ok:
        return False, reason
    previous_content = path.read_text(encoding="utf-8")
    path.write_text(new_content, encoding="utf-8")
    ok, reason = _validate_java_build_if_available(path, root)
    if not ok:
        path.write_text(previous_content, encoding="utf-8")
        return False, reason
    return True, "applied"


def undo_suggestion(suggestion: dict[str, Any], root: Path | None = None) -> tuple[bool, str]:
    located = _locate_proposed_block(suggestion, root)
    if located is None:
        if _locate_original_block(suggestion, root) is not None:
            return True, "already open"
        return False, "proposed_code does not match the current file content"

    path, start, end, proposed_block, expected = located
    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    original = _rebase_proposed_indent(str(suggestion.get("original_code") or ""), expected, proposed_block)
    original = _with_original_trailing_newline(original, proposed_block)
    new_content = "".join(lines[:start - 1]) + original + "".join(lines[end:])
    path.write_text(new_content, encoding="utf-8")
    return True, "undone"


def apply_suggestions(suggestions: list[dict[str, Any]], root: Path | None = None) -> dict[str, Any]:
    ordered = sorted(
        suggestions,
        key=lambda item: (str(item.get("file", "")), int(item.get("original_start_line") or item.get("line") or 0)),
        reverse=True,
    )
    results = []
    for suggestion in ordered:
        applied, message = apply_suggestion(suggestion, root)
        results.append({
            "id": suggestion.get("id"),
            "file": suggestion.get("file"),
            "applied": applied,
            "message": message,
        })
    return {
        "applied": len([item for item in results if item["applied"]]),
        "skipped": len([item for item in results if not item["applied"]]),
        "failed": 0,
        "results": results,
    }


def undo_suggestions(suggestions: list[dict[str, Any]], root: Path | None = None) -> dict[str, Any]:
    ordered = sorted(
        suggestions,
        key=lambda item: (str(item.get("file", "")), int(item.get("original_start_line") or item.get("line") or 0)),
        reverse=True,
    )
    results = []
    for suggestion in ordered:
        undone, message = undo_suggestion(suggestion, root)
        results.append({
            "id": suggestion.get("id"),
            "file": suggestion.get("file"),
            "applied": undone,
            "message": message,
        })
    return {
        "applied": len([item for item in results if item["applied"]]),
        "skipped": len([item for item in results if not item["applied"]]),
        "failed": 0,
        "results": results,
    }


def suggestion_status(suggestion: dict[str, Any], root: Path | None = None) -> str:
    proposed = _locate_proposed_block(suggestion, root)
    if proposed is not None:
        path, *_ = proposed
        if required_edits_present(path.read_text(encoding="utf-8"), suggestion, path):
            return "applied"
        return "changed"
    if _locate_original_block(suggestion, root) is not None:
        return "open"
    return "changed"


def suggestion_statuses(suggestions: list[dict[str, Any]], root: Path | None = None) -> list[dict[str, str]]:
    return [
        {
            "id": str(suggestion.get("id") or ""),
            "file": str(suggestion.get("file") or ""),
            "status": suggestion_status(suggestion, root),
        }
        for suggestion in suggestions
    ]


def print_summary(summary: dict[str, Any]) -> None:
    print(f"Applied: {summary['applied']} | Skipped: {summary['skipped']} | Failed: {summary['failed']}")
    for result in summary["results"]:
        status = "APPLIED" if result["applied"] else "SKIPPED"
        print(f"{status} {result['id']} {result['file']} - {result['message']}")


def _short(text: str, limit: int = 80) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else text[:limit - 3] + "..."


def command_list(args: argparse.Namespace) -> int:
    data = load_results_file(args.file)
    suggestions = filter_suggestions(
        data.get("suggestions", []),
        severity=args.severity or "",
        source=args.source or "",
        path=args.path or "",
        current_file=args.current_file or "",
        limit=args.limit or 0,
    )
    print(f"{'ID':<18} {'Severity':<10} {'File':<35} {'Line':<5} {'Target':<20} Problem")
    for item in suggestions:
        print(
            f"{str(item.get('id', '')):<18} "
            f"{str(item.get('severity', '')):<10} "
            f"{str(item.get('file', '')):<35} "
            f"{str(item.get('line', '')):<5} "
            f"{_short(item.get('target_name', ''), 20):<20} "
            f"{_short(item.get('problem', ''))}"
        )
    return 0


def command_show(args: argparse.Namespace) -> int:
    data = load_results_file(args.file)
    item = find_suggestion(data, args.id)
    print(f"ID: {item.get('id')}")
    print(f"Source: {item.get('source')}")
    print(f"Severity: {item.get('severity')}")
    print(f"File: {item.get('file')}:{item.get('line')}")
    print(f"Target: {item.get('target_type')} {item.get('target_name')}")
    print("\nProblem:\n" + str(item.get("problem") or ""))
    print("\nSolution:\n" + str(item.get("solution") or ""))
    print("\nOriginal code:\n" + str(item.get("original_code") or ""))
    print("\nProposed code:\n" + str(item.get("proposed_code") or ""))
    imports = item.get("required_imports") or []
    if imports:
        print("\nRequired imports:\n" + "\n".join(imports))
    removed_imports = item.get("optional_removed_imports") or []
    if removed_imports:
        print("\nOptional removed imports:\n" + "\n".join(removed_imports))
    auxiliary_edits = item.get("auxiliary_edits") or []
    if auxiliary_edits:
        print("\nAuxiliary edits:")
        for index, edit in enumerate(auxiliary_edits, start=1):
            print(f"\n[{index}] {edit.get('description') or 'Auxiliary edit'}")
            print(str(edit.get("original_code") or ""))
            print("->")
            print(str(edit.get("proposed_code") or ""))
    print(f"\nContent hash: {item.get('content_hash')}")
    return 0


def command_validate(args: argparse.Namespace) -> int:
    data = load_results_file(args.file)
    item = find_suggestion(data, args.id)
    ok, reason = validate_local_match(item)
    print("valid" if ok else f"invalid: {reason}")
    return 0 if ok else 1


def command_status(args: argparse.Namespace) -> int:
    data = load_results_file(args.file)
    suggestions = data.get("suggestions", [])
    if args.id:
        suggestions = [find_suggestion(data, args.id)]
    print(json.dumps({"suggestions": suggestion_statuses(suggestions)}, indent=2))
    return 0


def command_apply(args: argparse.Namespace) -> int:
    data = load_results_file(args.file)
    item = find_suggestion(data, args.id)
    summary = apply_suggestions([item])
    print_summary(summary)
    return 0 if summary["applied"] == 1 else 1


def command_apply_selected(args: argparse.Namespace) -> int:
    data = load_results_file(args.file)
    ids = [value.strip() for value in args.ids.split(",") if value.strip()]
    suggestions = [find_suggestion(data, suggestion_id) for suggestion_id in ids]
    summary = apply_suggestions(suggestions)
    print_summary(summary)
    return 0 if summary["applied"] > 0 else 1


def command_undo(args: argparse.Namespace) -> int:
    data = load_results_file(args.file)
    item = find_suggestion(data, args.id)
    summary = undo_suggestions([item])
    print_summary(summary)
    return 0 if summary["applied"] == 1 else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="CodeGuardian local suggestions CLI")
    subparsers = parser.add_subparsers(dest="command", required=True)

    list_parser = subparsers.add_parser("list", help="List suggestions")
    list_parser.add_argument("--file", required=True)
    list_parser.add_argument("--severity")
    list_parser.add_argument("--source")
    list_parser.add_argument("--path")
    list_parser.add_argument("--current-file")
    list_parser.add_argument("--limit", type=int, default=0)
    list_parser.set_defaults(func=command_list)

    show_parser = subparsers.add_parser("show", help="Show one suggestion")
    show_parser.add_argument("--file", required=True)
    show_parser.add_argument("--id", required=True)
    show_parser.set_defaults(func=command_show)

    apply_parser = subparsers.add_parser("apply", help="Apply one suggestion")
    apply_parser.add_argument("--file", required=True)
    apply_parser.add_argument("--id", required=True)
    apply_parser.set_defaults(func=command_apply)

    selected_parser = subparsers.add_parser("apply-selected", help="Apply selected suggestions")
    selected_parser.add_argument("--file", required=True)
    selected_parser.add_argument("--ids", required=True)
    selected_parser.set_defaults(func=command_apply_selected)

    undo_parser = subparsers.add_parser("undo", help="Undo one applied suggestion")
    undo_parser.add_argument("--file", required=True)
    undo_parser.add_argument("--id", required=True)
    undo_parser.set_defaults(func=command_undo)

    validate_parser = subparsers.add_parser("validate", help="Validate one suggestion")
    validate_parser.add_argument("--file", required=True)
    validate_parser.add_argument("--id", required=True)
    validate_parser.set_defaults(func=command_validate)

    status_parser = subparsers.add_parser("status", help="Return suggestion apply status")
    status_parser.add_argument("--file", required=True)
    status_parser.add_argument("--id")
    status_parser.set_defaults(func=command_status)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
