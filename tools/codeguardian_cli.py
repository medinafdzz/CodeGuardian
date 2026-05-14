#!/usr/bin/env python3
import argparse
import json
import sys
from pathlib import Path
from typing import Any


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


def _line_ending(text: str) -> str:
    return "\r\n" if "\r\n" in text else "\n"


def _with_original_trailing_newline(proposed: str, original_block: str) -> str:
    if original_block.endswith(("\r\n", "\n")) and not proposed.endswith(("\r\n", "\n")):
        return proposed + _line_ending(original_block)
    return proposed


def validate_local_match(suggestion: dict[str, Any], root: Path | None = None) -> tuple[bool, str]:
    path = _repo_path(str(suggestion.get("file", "")), root)
    if not path.is_file():
        return False, f"File not found: {path}"

    start = int(suggestion.get("original_start_line") or suggestion.get("line") or 0)
    end = int(suggestion.get("original_end_line") or suggestion.get("line") or 0)
    if start < 1 or end < start:
        return False, "Invalid original line range"

    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    if end > len(lines):
        return False, "Original line range is outside the current file"

    current = "".join(lines[start - 1:end])
    expected = str(suggestion.get("original_code") or "")
    if current.rstrip("\r\n") != expected.rstrip("\r\n"):
        return False, "original_code does not match the current file content in the expected line range"

    return True, ""


def apply_suggestion(suggestion: dict[str, Any], root: Path | None = None) -> tuple[bool, str]:
    ok, reason = validate_local_match(suggestion, root)
    if not ok:
        return False, reason

    path = _repo_path(str(suggestion.get("file", "")), root)
    start = int(suggestion.get("original_start_line") or suggestion.get("line") or 0)
    end = int(suggestion.get("original_end_line") or suggestion.get("line") or 0)
    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    original_block = "".join(lines[start - 1:end])
    proposed = _with_original_trailing_newline(str(suggestion.get("proposed_code") or ""), original_block)
    new_content = "".join(lines[:start - 1]) + proposed + "".join(lines[end:])
    path.write_text(new_content, encoding="utf-8")
    return True, "applied"


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
    print(f"\nContent hash: {item.get('content_hash')}")
    return 0


def command_validate(args: argparse.Namespace) -> int:
    data = load_results_file(args.file)
    item = find_suggestion(data, args.id)
    ok, reason = validate_local_match(item)
    print("valid" if ok else f"invalid: {reason}")
    return 0 if ok else 1


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

    validate_parser = subparsers.add_parser("validate", help="Validate one suggestion")
    validate_parser.add_argument("--file", required=True)
    validate_parser.add_argument("--id", required=True)
    validate_parser.set_defaults(func=command_validate)

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
