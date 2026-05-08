import os
import re
from functools import lru_cache

from codeguardian.models import ScopeInfo


@lru_cache(maxsize=256)
def read_file_lines(filepath: str) -> list[str]:
    if not os.path.exists(filepath):
        raise FileNotFoundError("File not found.")
    with open(filepath, "r", encoding="utf-8") as file:
        return file.readlines()


def clean_replacement_text(value: str) -> str:
    return value.replace('\\n', '\n').strip('`').strip()


def normalize_code_block(text: str) -> str:
    if not text:
        return ""
    return "\n".join(line.rstrip() for line in text.replace("\r\n", "\n").strip().splitlines()).strip()


def detect_language(filepath: str) -> str:
    ext = os.path.splitext(filepath)[1].lower()
    mapping = {
        ".py": "python",
        ".java": "java",
        ".js": "javascript",
        ".ts": "typescript",
        ".go": "go",
        ".cs": "csharp",
        ".cpp": "cpp",
        ".cxx": "cpp",
        ".cc": "cpp",
        ".c": "c",
        ".php": "php",
        ".rb": "ruby",
        ".rs": "rust",
        ".kt": "kotlin",
        ".swift": "swift",
    }
    return mapping.get(ext, "unknown")


def resolve_scope_with_parser(filepath: str, line_number: int, language: str) -> ScopeInfo:

    lines = read_file_lines(filepath)

    if not lines:
        return ScopeInfo("global", "", line_number, line_number)

    line_number = max(1, min(line_number, len(lines)))

    if language == "python":
        for start_idx in range(line_number - 1, -1, -1):
            match = re.match(
                r"^([ \t]*)(?:async\s+def|def)\s+([A-Za-z_]\w*)\s*\(",
                lines[start_idx],
            )
            if not match:
                continue

            base_indent = len(match.group(1).replace("\t", "    "))
            end_line = len(lines)

            for end_idx in range(start_idx + 1, len(lines)):
                candidate = lines[end_idx]
                stripped = candidate.strip()

                if not stripped:
                    continue

                candidate_indent = len(candidate[:len(candidate) - len(candidate.lstrip(" \t"))].replace("\t", "    "))

                if candidate_indent <= base_indent:
                    end_line = end_idx
                    break

            if start_idx + 1 <= line_number <= end_line:
                return ScopeInfo("function", match.group(2), start_idx + 1, end_line)

        return ScopeInfo("global", "", line_number, line_number)

    if language not in {
            "java",
            "javascript",
            "typescript",
            "go",
            "csharp",
            "cpp",
            "c",
            "php",
            "rust",
            "kotlin",
            "swift",
    }:
        return ScopeInfo("global", "", line_number, line_number)

    scope_kind = "method" if language in {"java", "csharp", "kotlin", "swift", "php"} else "function"

    for start_idx in range(line_number - 1, -1, -1):
        signature_parts = []
        open_brace_line = None

        for cursor in range(start_idx, min(len(lines), start_idx + 6)):
            stripped = lines[cursor].strip()

            if not stripped and not signature_parts:
                break

            signature_parts.append(stripped)

            if "{" in stripped:
                open_brace_line = cursor
                break

            if ";" in stripped:
                break

        if open_brace_line is None:
            continue

        signature_text = " ".join(signature_parts).strip()

        if "(" not in signature_text or ")" not in signature_text:
            continue

        if re.match(
                r"^(if|for|foreach|while|switch|catch|else|do|try|using|lock|with|synchronized)\b",
                signature_text,
        ):
            continue

        if re.search(r"\bnew\s+[A-Za-z_][\w$]*\s*\([^()]*\)\s*\{", signature_text):
            continue

        name_match = re.search(
            r"([A-Za-z_][\w$]*)\s*\([^()]*\)\s*(?:throws\b[^{}]*)?\{",
            signature_text,
        )

        if not name_match and language in {"javascript", "typescript"}:
            name_match = re.search(
                r"([A-Za-z_$][\w$]*)\s*=\s*(?:async\s*)?(?:\([^)]*\)|[A-Za-z_$][\w$]*)\s*=>\s*\{",
                signature_text,
            )

        if not name_match:
            continue

        scope_name = name_match.group(1)
        if scope_name in {
                "if",
                "for",
                "foreach",
                "while",
                "switch",
                "catch",
                "else",
                "do",
                "try",
                "using",
                "lock",
                "with",
                "synchronized",
        }:
            continue

        brace_depth = 0
        entered_scope = False
        end_line = None

        for end_idx in range(open_brace_line, len(lines)):
            brace_depth += lines[end_idx].count("{")
            brace_depth -= lines[end_idx].count("}")

            if end_idx + 1 >= line_number and brace_depth > 0:
                entered_scope = True

            if brace_depth == 0:
                end_line = end_idx + 1
                break

        if entered_scope and end_line is not None and start_idx + 1 <= line_number <= end_line:
            return ScopeInfo(scope_kind, scope_name, start_idx + 1, end_line)

    return ScopeInfo("global", "", line_number, line_number)


def resolve_scope(filepath: str, line_number: int) -> ScopeInfo:
    language = detect_language(filepath)

    try:
        if language == "unknown":
            return ScopeInfo("global", "", line_number, line_number)

        return resolve_scope_with_parser(filepath, line_number, language)

    except Exception:
        return ScopeInfo("global", "", line_number, line_number)


def get_code_context(filepath: str, line_number: int, context_window: int = 20) -> str:
    try:
        lines = read_file_lines(filepath)

        # Calculate the start and end lines for the code snippet
        start_line = max(0, line_number - context_window - 1)  # -1 because line numbers are typically 1-indexed
        end_line = min(len(lines), line_number + context_window)

        snippet = []
        for i in range(start_line, end_line):
            # Here I mark visually the error line
            prefix = ">> " if (i + 1) == line_number else "   "
            snippet.append(f"{prefix}{i + 1}: {lines[i].rstrip()}")

        return "\n".join(snippet)
    except Exception as e:
        return f"Error reading code context: {e}"
