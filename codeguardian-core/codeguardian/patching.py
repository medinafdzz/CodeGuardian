import ast
import re
from pathlib import Path
from typing import Any


def _get(source: Any, key: str, default: Any = None) -> Any:
    if isinstance(source, dict):
        return source.get(key, default)
    return getattr(source, key, default)


def _as_text_list(value: Any) -> list[str]:
    if not value:
        return []
    if not isinstance(value, list):
        return []
    result = []
    for item in value:
        text = str(item or "").strip()
        if text:
            result.append(text)
    return result


def language_for_path(file_path: str | Path) -> str:
    suffix = Path(str(file_path)).suffix.lower()
    if suffix == ".py":
        return "python"
    if suffix == ".java":
        return "java"
    return ""


def validate_import_line(import_line: str, language: str) -> tuple[bool, str]:
    line = import_line.strip()
    if not line:
        return False, "empty import line"
    if language == "python":
        if not (line.startswith("import ") or line.startswith("from ")):
            return False, f"invalid python import: {line}"
        if re.search(r"\bimport\s+\*", line):
            return False, f"wildcard python import is not allowed: {line}"
        return True, ""
    if language == "java":
        if not line.startswith("import ") or not line.endswith(";"):
            return False, f"invalid java import: {line}"
        if ".*;" in line:
            return False, f"wildcard java import is not allowed: {line}"
        return True, ""
    return False, f"unsupported import language for {line}"


def _line_ending(text: str) -> str:
    return "\r\n" if "\r\n" in text else "\n"


def _split_keepends(text: str) -> list[str]:
    return text.splitlines(keepends=True)


def _line_without_ending(line: str) -> str:
    return line.rstrip("\r\n")


def _has_line(content: str, expected_line: str) -> bool:
    expected = expected_line.strip()
    return any(_line_without_ending(line).strip() == expected for line in _split_keepends(content))


def _ensure_line_ending(line: str, newline: str) -> str:
    stripped = line.rstrip("\r\n")
    return stripped + newline


def _python_docstring_end_line(content: str) -> int:
    try:
        module = ast.parse(content)
    except SyntaxError:
        return 0
    if not module.body:
        return 0
    first = module.body[0]
    if isinstance(first, ast.Expr) and isinstance(getattr(first, "value", None), ast.Constant):
        if isinstance(first.value.value, str):
            return int(getattr(first, "end_lineno", 0) or 0)
    return 0


def _python_import_insert_index(lines: list[str], content: str) -> int:
    index = 0
    if index < len(lines) and lines[index].startswith("#!"):
        index += 1
    if index < len(lines) and re.match(r"#.*coding[:=]\s*[-\w.]+", lines[index]):
        index += 1

    doc_end = _python_docstring_end_line(content)
    if doc_end > index:
        index = doc_end

    while index < len(lines) and not lines[index].strip():
        index += 1

    while index < len(lines) and lines[index].strip().startswith("from __future__ import "):
        index += 1

    scan = index
    while scan < len(lines) and not lines[scan].strip():
        scan += 1

    if scan >= len(lines) or not (lines[scan].strip().startswith("import ") or lines[scan].strip().startswith("from ")):
        return index

    index = scan
    while index < len(lines):
        stripped = lines[index].strip()
        if stripped.startswith("import ") or stripped.startswith("from ") or not stripped:
            index += 1
            continue
        break
    return index


def _java_import_insert_index(lines: list[str]) -> int:
    index = 0
    package_index = -1
    last_import_index = -1
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("package ") and stripped.endswith(";"):
            package_index = i
        if stripped.startswith("import ") and stripped.endswith(";"):
            last_import_index = i

    if last_import_index >= 0:
        return last_import_index + 1
    if package_index >= 0:
        return package_index + 1
    return index


def add_required_imports(content: str, file_path: str | Path, required_imports: list[str]) -> tuple[bool, str, str]:
    language = language_for_path(file_path)
    imports = list(dict.fromkeys(_as_text_list(required_imports)))
    if not imports:
        return True, content, ""
    if language not in {"python", "java"}:
        return False, content, "required imports are only supported for Java and Python files"

    for import_line in imports:
        ok, reason = validate_import_line(import_line, language)
        if not ok:
            return False, content, reason

    newline = _line_ending(content)
    lines = _split_keepends(content)
    missing = [line for line in imports if not _has_line(content, line)]
    if not missing:
        return True, content, ""

    insert_index = _python_import_insert_index(lines, content) if language == "python" else _java_import_insert_index(lines)
    remainder_index = insert_index
    insertion = [_ensure_line_ending(line, newline) for line in missing]

    if language == "python":
        while remainder_index < len(lines) and not lines[remainder_index].strip():
            remainder_index += 1
        insertion.append(newline)

    if language == "java":
        if insert_index < len(lines) and lines[insert_index].strip() and not lines[insert_index].strip().startswith("import "):
            insertion.append(newline)
        if insert_index > 0 and lines[insert_index - 1].strip().startswith("package "):
            insertion.insert(0, newline)

    new_content = "".join(lines[:insert_index] + insertion + lines[remainder_index:])
    return True, new_content, ""


def remove_optional_imports(content: str, optional_removed_imports: list[str]) -> str:
    imports = set(_as_text_list(optional_removed_imports))
    if not imports:
        return content
    return "".join(
        line for line in _split_keepends(content)
        if _line_without_ending(line).strip() not in imports
    )


def _normalized_block(text: str) -> str:
    return "\n".join(line.strip() for line in text.rstrip("\r\n").splitlines()).strip()


def _replace_unique_block(content: str, original: str, proposed: str) -> tuple[bool, str, str]:
    if not original:
        return False, content, "auxiliary edit is missing original_code"
    if content.count(original) == 1:
        return True, content.replace(original, proposed, 1), ""

    original_norm = _normalized_block(original)
    if not original_norm:
        return False, content, "auxiliary edit is empty"

    lines = _split_keepends(content)
    original_lines = original.rstrip("\r\n").splitlines()
    size = len(original_lines)
    matches: list[tuple[int, int]] = []
    for start in range(0, len(lines) - size + 1):
        candidate = "".join(lines[start:start + size])
        if _normalized_block(candidate) == original_norm:
            matches.append((start, start + size))

    if len(matches) != 1:
        return False, content, "auxiliary edit is ambiguous or does not match"

    start, end = matches[0]
    current = "".join(lines[start:end])
    replacement = proposed
    if current.endswith(("\r\n", "\n")) and replacement and not replacement.endswith(("\r\n", "\n")):
        replacement += _line_ending(current)
    return True, "".join(lines[:start]) + replacement + "".join(lines[end:]), ""


def apply_auxiliary_edits(content: str, suggestion: Any) -> tuple[bool, str, str]:
    edits = _get(suggestion, "auxiliary_edits", []) or []
    for edit in edits:
        original = _get(edit, "original_code", "")
        proposed = _get(edit, "proposed_code", "")
        ok, content, reason = _replace_unique_block(content, str(original or ""), str(proposed or ""))
        if not ok:
            return False, content, reason
    return True, content, ""


def apply_additional_file_edits(content: str, suggestion: Any, file_path: str | Path) -> tuple[bool, str, str]:
    content = remove_optional_imports(content, _get(suggestion, "optional_removed_imports", []) or [])
    ok, content, reason = apply_auxiliary_edits(content, suggestion)
    if not ok:
        return False, content, reason
    return add_required_imports(content, file_path, _get(suggestion, "required_imports", []) or [])


def validate_patched_text(content: str, file_path: str | Path) -> tuple[bool, str]:
    language = language_for_path(file_path)
    if language == "python":
        try:
            ast.parse(content)
        except SyntaxError as exc:
            return False, f"python syntax validation failed: {exc}"
    return True, ""


def required_edits_present(content: str, suggestion: Any, file_path: str | Path) -> bool:
    imports = _as_text_list(_get(suggestion, "required_imports", []) or [])
    if imports and not all(_has_line(content, line) for line in imports):
        return False

    for edit in _get(suggestion, "auxiliary_edits", []) or []:
        proposed = str(_get(edit, "proposed_code", "") or "")
        if proposed and _normalized_block(proposed) not in _normalized_block(content):
            return False

    return True
