#!/usr/bin/env python3
import argparse
import base64
import hashlib
import json
import os
import stat
import sys
import tempfile
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from codeguardian.patching import apply_additional_file_edits, required_edits_present, validate_patched_text


def load_results_file(path: str | Path) -> dict[str, Any]:
    return json.loads(_read_text(Path(path)))


def _read_text(path: Path) -> str:
    with path.open("r", encoding="utf-8", newline="") as stream:
        return stream.read()


def _atomic_write_bytes(path: Path, content: bytes, mode: int | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        target_mode = mode
        if target_mode is None:
            target_mode = stat.S_IMODE(path.stat().st_mode) if path.exists() else 0o600
        os.chmod(temporary_path, target_mode)
        os.replace(temporary_path, path)
    except Exception:
        temporary_path.unlink(missing_ok=True)
        raise


def _atomic_write_text(path: Path, content: str, mode: int | None = None) -> None:
    _atomic_write_bytes(path, content.encode("utf-8"), mode)


class AmbiguousBlockError(ValueError):
    pass


class UndoStateError(RuntimeError):
    pass


UNDO_STATE_SCHEMA_VERSION = 1
UNDO_STATE_DIRECTORY = ".codeguardian"
UNDO_STATE_FILENAME = "undo-state.json"
UNDO_STATE_LOCK_FILENAME = "undo-state.lock"


def _repo_root(root: Path | None = None) -> Path:
    return (root or Path.cwd()).resolve()


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _encode_snapshot(content: bytes) -> str:
    return base64.b64encode(content).decode("ascii")


def _decode_snapshot(value: Any) -> bytes:
    try:
        return base64.b64decode(str(value), validate=True)
    except (ValueError, TypeError) as exc:
        raise UndoStateError("CodeGuardian undo state contains an invalid file snapshot") from exc


def _undo_state_path(root: Path | None = None) -> Path:
    return _repo_root(root) / UNDO_STATE_DIRECTORY / UNDO_STATE_FILENAME


def _git_directory(root: Path) -> Path | None:
    marker = root / ".git"
    if marker.is_dir():
        return marker
    if marker.is_file():
        try:
            value = marker.read_text(encoding="utf-8").strip()
        except OSError:
            return None
        if value.lower().startswith("gitdir:"):
            candidate = Path(value.split(":", 1)[1].strip())
            return (candidate if candidate.is_absolute() else root / candidate).resolve()
    return None


def _ensure_state_excluded_from_git(root: Path) -> None:
    git_directory = _git_directory(root)
    if git_directory is None:
        return
    exclude_path = git_directory / "info" / "exclude"
    exclude_path.parent.mkdir(parents=True, exist_ok=True)
    current = exclude_path.read_text(encoding="utf-8") if exclude_path.exists() else ""
    entry = f"/{UNDO_STATE_DIRECTORY}/"
    if entry not in {line.strip() for line in current.splitlines()}:
        separator = "" if not current or current.endswith(("\n", "\r")) else "\n"
        _atomic_write_text(exclude_path, f"{current}{separator}{entry}\n", mode=0o600)


def _new_undo_state() -> dict[str, Any]:
    return {
        "schema_version": UNDO_STATE_SCHEMA_VERSION,
        "next_sequence": 1,
        "transactions": [],
    }


def _load_undo_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return _new_undo_state()
    try:
        state = json.loads(_read_text(path))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise UndoStateError(f"CodeGuardian undo state is unreadable or corrupt: {path}") from exc
    if not isinstance(state, dict) or state.get("schema_version") != UNDO_STATE_SCHEMA_VERSION:
        raise UndoStateError(f"Unsupported CodeGuardian undo state schema: {path}")
    if not isinstance(state.get("transactions"), list):
        raise UndoStateError(f"CodeGuardian undo state has an invalid transaction list: {path}")
    if not isinstance(state.get("next_sequence"), int):
        raise UndoStateError(f"CodeGuardian undo state has an invalid sequence: {path}")
    return state


def _save_undo_state(path: Path, state: dict[str, Any]) -> None:
    serialized = json.dumps(state, ensure_ascii=False, indent=2) + "\n"
    _atomic_write_text(path, serialized, mode=0o600)


@contextmanager
def _locked_undo_state(root: Path | None = None):
    repository_root = _repo_root(root)
    state_path = _undo_state_path(repository_root)
    state_path.parent.mkdir(parents=True, exist_ok=True)
    _ensure_state_excluded_from_git(repository_root)
    lock_path = state_path.parent / UNDO_STATE_LOCK_FILENAME
    deadline = time.monotonic() + 5.0
    descriptor: int | None = None
    while descriptor is None:
        try:
            descriptor = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            try:
                stale = time.time() - lock_path.stat().st_mtime > 120
            except FileNotFoundError:
                continue
            if stale:
                lock_path.unlink(missing_ok=True)
                continue
            if time.monotonic() >= deadline:
                raise UndoStateError("CodeGuardian undo state is locked by another operation")
            time.sleep(0.05)
    try:
        os.write(descriptor, f"pid={os.getpid()} created={_utc_now()}\n".encode("utf-8"))
        os.close(descriptor)
        descriptor = None
        yield _load_undo_state(state_path), state_path
    finally:
        if descriptor is not None:
            os.close(descriptor)
        lock_path.unlink(missing_ok=True)


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
    repository_root = _repo_root(root)
    path = Path(file_path)
    candidate = (path if path.is_absolute() else repository_root / path).resolve()
    try:
        candidate.relative_to(repository_root)
    except ValueError as exc:
        raise ValueError(f"File path escapes the repository: {file_path}") from exc
    return candidate


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

    if len(matches) > 1:
        raise AmbiguousBlockError("code block matches more than one location")

    matches.sort(key=lambda item: abs(item[0] - preferred_start))
    return matches[0]


def _locate_block(
    suggestion: dict[str, Any],
    block: str,
    root: Path | None = None,
    *,
    path: Path | None = None,
    content: str | None = None,
) -> tuple[Path, int, int, str, str] | None:
    path = path or _repo_path(str(suggestion.get("file", "")), root)
    if not path.is_file():
        return None

    start, end = _line_range(suggestion)
    if start < 1 or end < start:
        return None

    if content is None:
        content = _read_text(path)
    lines = content.splitlines(keepends=True)
    if end <= len(lines):
        current = "".join(lines[start - 1:end])
        if _blocks_match(current, block):
            return path, start, end, current, block

    found = _find_matching_range(lines, block, start)
    if found:
        found_start, found_end, current = found
        return path, found_start, found_end, current, block

    return None


def _locate_original_block(
    suggestion: dict[str, Any],
    root: Path | None = None,
    *,
    path: Path | None = None,
    content: str | None = None,
) -> tuple[Path, int, int, str, str] | None:
    return _locate_block(
        suggestion,
        str(suggestion.get("original_code") or ""),
        root,
        path=path,
        content=content,
    )


def _locate_proposed_block(
    suggestion: dict[str, Any],
    root: Path | None = None,
    *,
    path: Path | None = None,
    content: str | None = None,
) -> tuple[Path, int, int, str, str] | None:
    return _locate_block(
        suggestion,
        str(suggestion.get("proposed_code") or ""),
        root,
        path=path,
        content=content,
    )


def validate_local_match(suggestion: dict[str, Any], root: Path | None = None) -> tuple[bool, str]:
    path = _repo_path(str(suggestion.get("file", "")), root)
    if not path.is_file():
        return False, f"File not found: {path}"

    start, end = _line_range(suggestion)
    if start < 1 or end < start:
        return False, "Invalid original line range"

    content = _read_text(path)
    try:
        proposed = _locate_proposed_block(suggestion, root, path=path, content=content)
        original = _locate_original_block(suggestion, root, path=path, content=content)
    except AmbiguousBlockError as exc:
        return False, f"original_code is ambiguous: {exc}"

    if original is None and proposed is None:
        return False, "original_code does not match the current file content"

    if proposed is not None and not required_edits_present(content, suggestion, path):
        return False, "required imports or auxiliary edits are missing"

    return True, ""


def _result(
    suggestion: dict[str, Any],
    applied: bool,
    message: str,
    **details: Any,
) -> dict[str, Any]:
    return {
        "id": suggestion.get("id"),
        "file": suggestion.get("file"),
        "applied": applied,
        "message": message,
        **details,
    }


def _active_transactions(state: dict[str, Any], file_name: str) -> list[dict[str, Any]]:
    return sorted(
        [
            transaction
            for transaction in state["transactions"]
            if transaction.get("file") == file_name and transaction.get("status") == "committed"
        ],
        key=lambda transaction: int(transaction.get("sequence") or 0),
    )


def _active_suggestion_transaction(
    state: dict[str, Any],
    suggestion_id: str,
    file_name: str,
) -> dict[str, Any] | None:
    matching = [
        transaction
        for transaction in _active_transactions(state, file_name)
        if str(transaction.get("suggestion_id")) == suggestion_id
    ]
    return matching[-1] if matching else None


def _pending_suggestion_transaction(
    state: dict[str, Any],
    suggestion_id: str,
    file_name: str,
) -> dict[str, Any] | None:
    matching = [
        transaction
        for transaction in state["transactions"]
        if transaction.get("file") == file_name
        and transaction.get("status") == "pending"
        and str(transaction.get("suggestion_id")) == suggestion_id
    ]
    return max(matching, key=lambda transaction: int(transaction.get("sequence") or 0)) if matching else None


def _has_exact_line(content: str, expected: str) -> bool:
    normalized = expected.strip()
    return any(line.strip() == normalized for line in content.splitlines())


def _transaction_record(
    state: dict[str, Any],
    suggestion: dict[str, Any],
    file_name: str,
    before: bytes,
    after: bytes,
    mode: int,
    artifact_context: dict[str, Any] | None,
) -> dict[str, Any]:
    sequence = state["next_sequence"]
    state["next_sequence"] = sequence + 1
    before_text = before.decode("utf-8")
    after_text = after.decode("utf-8")
    required_imports = list(suggestion.get("required_imports") or [])
    optional_removed_imports = list(suggestion.get("optional_removed_imports") or [])
    imports_added = [
        value for value in required_imports
        if not _has_exact_line(before_text, str(value)) and _has_exact_line(after_text, str(value))
    ]
    imports_removed = [
        value for value in optional_removed_imports
        if _has_exact_line(before_text, str(value)) and not _has_exact_line(after_text, str(value))
    ]
    return {
        "transaction_id": str(uuid.uuid4()),
        "sequence": sequence,
        "status": "pending",
        "suggestion_id": str(suggestion.get("id") or ""),
        "file": file_name,
        "applied_at": _utc_now(),
        "before_hash": _sha256(before),
        "after_hash": _sha256(after),
        "before_content_b64": _encode_snapshot(before),
        "after_content_b64": _encode_snapshot(after),
        "before_mode": mode,
        "after_mode": mode,
        "artifact": dict(artifact_context or {}),
        "imports_added": imports_added,
        "imports_removed": imports_removed,
        "auxiliary_edits": list(suggestion.get("auxiliary_edits") or []),
    }


def _apply_suggestion_transaction(
    suggestion: dict[str, Any],
    root: Path | None = None,
    artifact_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    repository_root = _repo_root(root)
    try:
        target_path = _repo_path(str(suggestion.get("file", "")), repository_root)
    except ValueError as exc:
        return _result(suggestion, False, str(exc), blocked_reason=str(exc))
    if not target_path.is_file():
        return _result(suggestion, False, f"File not found: {target_path}")

    start_line, end_line = _line_range(suggestion)
    if start_line < 1 or end_line < start_line:
        return _result(suggestion, False, "Invalid original line range")

    try:
        with _locked_undo_state(repository_root) as (state, state_path):
            previous_bytes = target_path.read_bytes()
            try:
                previous_content = previous_bytes.decode("utf-8")
            except UnicodeDecodeError:
                return _result(suggestion, False, "Target file is not valid UTF-8")
            relative_path = target_path.relative_to(repository_root).as_posix()
            previous_hash = _sha256(previous_bytes)
            suggestion_id = str(suggestion.get("id") or "")
            pending = _pending_suggestion_transaction(state, suggestion_id, relative_path)
            if pending is not None:
                if previous_hash == pending.get("after_hash"):
                    pending["status"] = "committed"
                    pending["committed_at"] = _utc_now()
                    pending["recovered_after_interruption"] = True
                    _save_undo_state(state_path, state)
                    return _result(
                        suggestion,
                        True,
                        "already applied",
                        transaction_id=pending.get("transaction_id"),
                        state_path=str(state_path),
                        before_hash=pending.get("before_hash"),
                        after_hash=pending.get("after_hash"),
                    )
                if previous_hash == pending.get("before_hash"):
                    pending["status"] = "rolled_back"
                    pending["completed_at"] = _utc_now()
                    pending["failure_reason"] = "Recovered pending transaction before source write"
                    _save_undo_state(state_path, state)
                else:
                    reason = "Cannot apply safely because an interrupted transaction does not match the current file."
                    return _result(
                        suggestion,
                        False,
                        reason,
                        transaction_id=pending.get("transaction_id"),
                        blocked_reason=reason,
                    )
            active = _active_suggestion_transaction(
                state,
                suggestion_id,
                relative_path,
            )
            if active is not None:
                if previous_hash == active.get("after_hash"):
                    return _result(
                        suggestion,
                        True,
                        "already applied",
                        transaction_id=active.get("transaction_id"),
                        state_path=str(state_path),
                        before_hash=active.get("before_hash"),
                        after_hash=active.get("after_hash"),
                    )
                active_for_file = _active_transactions(state, relative_path)
                if not active_for_file or active_for_file[-1].get("transaction_id") != active.get("transaction_id"):
                    reason = "Cannot apply safely because a later CodeGuardian change exists for this file."
                    return _result(suggestion, False, reason, blocked_reason=reason)

                manually_reverted = previous_hash == active.get("before_hash")
                if not manually_reverted:
                    try:
                        manually_reverted = _locate_original_block(
                            suggestion,
                            repository_root,
                            path=target_path,
                            content=previous_content,
                        ) is not None
                    except AmbiguousBlockError:
                        manually_reverted = False

                if manually_reverted:
                    active["status"] = "manually_reverted"
                    active["completed_at"] = _utc_now()
                    active["manually_reverted_at"] = active["completed_at"]
                    active["manual_revert_matches_before_hash"] = previous_hash == active.get("before_hash")
                    _save_undo_state(state_path, state)
                else:
                    reason = "Cannot apply safely because the file has changed since this suggestion was applied."
                    return _result(suggestion, False, reason, blocked_reason=reason)

            try:
                located = _locate_original_block(
                    suggestion,
                    repository_root,
                    path=target_path,
                    content=previous_content,
                )
            except AmbiguousBlockError as exc:
                return _result(suggestion, False, f"original_code is ambiguous: {exc}")

            if located is None:
                try:
                    proposed = _locate_proposed_block(
                        suggestion,
                        repository_root,
                        path=target_path,
                        content=previous_content,
                    )
                except AmbiguousBlockError as exc:
                    return _result(suggestion, False, f"proposed_code is ambiguous: {exc}")
                if proposed is not None and required_edits_present(previous_content, suggestion, target_path):
                    return _result(suggestion, True, "already applied")
                if proposed is not None:
                    return _result(
                        suggestion,
                        False,
                        "proposed_code is present but required imports or auxiliary edits are missing",
                    )
                return _result(suggestion, False, "original_code does not match the current file content")

            path, start, end, original_block, expected = located
            lines = previous_content.splitlines(keepends=True)
            proposed = _rebase_proposed_indent(
                str(suggestion.get("proposed_code") or ""),
                expected,
                original_block,
            )
            proposed = _with_original_trailing_newline(proposed, original_block)
            new_content = "".join(lines[:start - 1]) + proposed + "".join(lines[end:])
            ok, new_content, reason = apply_additional_file_edits(new_content, suggestion, path)
            if not ok:
                return _result(suggestion, False, reason)
            ok, reason = validate_patched_text(new_content, path)
            if not ok:
                return _result(suggestion, False, reason)

            new_bytes = new_content.encode("utf-8")
            original_mode = stat.S_IMODE(target_path.stat().st_mode)
            transaction = _transaction_record(
                state,
                suggestion,
                relative_path,
                previous_bytes,
                new_bytes,
                original_mode,
                artifact_context,
            )
            state["transactions"].append(transaction)
            _save_undo_state(state_path, state)

            try:
                _atomic_write_bytes(path, new_bytes, original_mode)
            except OSError as exc:
                transaction["status"] = "rolled_back"
                transaction["completed_at"] = _utc_now()
                transaction["failure_reason"] = str(exc)
                _save_undo_state(state_path, state)
                return _result(
                    suggestion,
                    False,
                    str(exc),
                    transaction_id=transaction["transaction_id"],
                    state_path=str(state_path),
                    failed=True,
                )
            try:
                transaction["status"] = "committed"
                transaction["committed_at"] = _utc_now()
                _save_undo_state(state_path, state)
            except Exception:
                _atomic_write_bytes(path, previous_bytes, original_mode)
                raise
            return _result(
                suggestion,
                True,
                "applied",
                transaction_id=transaction["transaction_id"],
                state_path=str(state_path),
                before_hash=transaction["before_hash"],
                after_hash=transaction["after_hash"],
            )
    except (OSError, UndoStateError) as exc:
        return _result(suggestion, False, str(exc), failed=True, blocked_reason=str(exc))


def apply_suggestion(
    suggestion: dict[str, Any],
    root: Path | None = None,
    artifact_context: dict[str, Any] | None = None,
) -> tuple[bool, str]:
    outcome = _apply_suggestion_transaction(suggestion, root, artifact_context)
    return bool(outcome["applied"]), str(outcome["message"])


def _undo_suggestion_transaction(
    suggestion: dict[str, Any],
    root: Path | None = None,
) -> dict[str, Any]:
    repository_root = _repo_root(root)
    try:
        path = _repo_path(str(suggestion.get("file", "")), repository_root)
    except ValueError as exc:
        return _result(suggestion, False, str(exc), blocked_reason=str(exc))
    if not path.is_file():
        return _result(suggestion, False, f"File not found: {path}")

    try:
        with _locked_undo_state(repository_root) as (state, state_path):
            relative_path = path.relative_to(repository_root).as_posix()
            transaction = _active_suggestion_transaction(
                state,
                str(suggestion.get("id") or ""),
                relative_path,
            )
            if transaction is None:
                current_content = _read_text(path)
                try:
                    original = _locate_original_block(
                        suggestion,
                        repository_root,
                        path=path,
                        content=current_content,
                    )
                except AmbiguousBlockError:
                    original = None
                if original is not None:
                    return _result(suggestion, True, "already open", restored=False)
                reason = "Cannot undo safely because no committed transaction exists for this suggestion."
                return _result(suggestion, False, reason, blocked_reason=reason, restored=False)

            active_for_file = _active_transactions(state, relative_path)
            if not active_for_file or active_for_file[-1].get("transaction_id") != transaction.get("transaction_id"):
                reason = "Cannot undo safely because a later CodeGuardian change exists for this file."
                return _result(
                    suggestion,
                    False,
                    reason,
                    transaction_id=transaction.get("transaction_id"),
                    blocked_reason=reason,
                    restored=False,
                )

            current_bytes = path.read_bytes()
            current_hash = _sha256(current_bytes)
            if current_hash != transaction.get("after_hash"):
                reason = "Cannot undo safely because the file has changed since this suggestion was applied."
                return _result(
                    suggestion,
                    False,
                    reason,
                    transaction_id=transaction.get("transaction_id"),
                    blocked_reason=reason,
                    state_path=str(state_path),
                    before_hash=transaction.get("before_hash"),
                    after_hash=transaction.get("after_hash"),
                    restored=False,
                )

            before_bytes = _decode_snapshot(transaction.get("before_content_b64"))
            after_bytes = _decode_snapshot(transaction.get("after_content_b64"))
            if _sha256(before_bytes) != transaction.get("before_hash") or _sha256(after_bytes) != transaction.get("after_hash"):
                raise UndoStateError("CodeGuardian undo state snapshot hashes do not match")
            try:
                before_content = before_bytes.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise UndoStateError("CodeGuardian undo snapshot is not valid UTF-8") from exc
            ok, reason = validate_patched_text(before_content, path)
            if not ok:
                return _result(
                    suggestion,
                    False,
                    reason,
                    transaction_id=transaction.get("transaction_id"),
                    blocked_reason=reason,
                    restored=False,
                )

            before_mode = int(transaction.get("before_mode") or stat.S_IMODE(path.stat().st_mode))
            _atomic_write_bytes(path, before_bytes, before_mode)

            transaction["status"] = "undone"
            transaction["undone_at"] = _utc_now()
            try:
                _save_undo_state(state_path, state)
            except Exception:
                transaction["status"] = "committed"
                transaction.pop("undone_at", None)
                _atomic_write_bytes(path, current_bytes, after_mode)
                raise
            return _result(
                suggestion,
                True,
                "undone",
                transaction_id=transaction.get("transaction_id"),
                state_path=str(state_path),
                before_hash=transaction.get("before_hash"),
                after_hash=transaction.get("after_hash"),
                restored=True,
            )
    except (OSError, UndoStateError) as exc:
        return _result(suggestion, False, str(exc), failed=True, blocked_reason=str(exc), restored=False)


def undo_suggestion(suggestion: dict[str, Any], root: Path | None = None) -> tuple[bool, str]:
    outcome = _undo_suggestion_transaction(suggestion, root)
    return bool(outcome["applied"]), str(outcome["message"])


def apply_suggestions(
    suggestions: list[dict[str, Any]],
    root: Path | None = None,
    artifact_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    ordered = sorted(
        suggestions,
        key=lambda item: (str(item.get("file", "")), int(item.get("original_start_line") or item.get("line") or 0)),
        reverse=True,
    )
    results = []
    for suggestion in ordered:
        results.append(_apply_suggestion_transaction(suggestion, root, artifact_context))
    return {
        "applied": len([item for item in results if item["applied"]]),
        "skipped": len([item for item in results if not item["applied"] and not item.get("failed")]),
        "failed": len([item for item in results if item.get("failed")]),
        "results": results,
    }


def undo_suggestions(suggestions: list[dict[str, Any]], root: Path | None = None) -> dict[str, Any]:
    repository_root = _repo_root(root)
    sequence_by_key: dict[tuple[str, str], int] = {}
    try:
        with _locked_undo_state(repository_root) as (state, _state_path):
            for transaction in state["transactions"]:
                if transaction.get("status") != "committed":
                    continue
                key = (str(transaction.get("suggestion_id") or ""), str(transaction.get("file") or ""))
                sequence_by_key[key] = max(sequence_by_key.get(key, 0), int(transaction.get("sequence") or 0))
    except UndoStateError:
        sequence_by_key = {}

    def undo_order(item: dict[str, Any]) -> tuple[int, int]:
        try:
            file_name = _repo_path(str(item.get("file", "")), repository_root).relative_to(repository_root).as_posix()
        except ValueError:
            file_name = str(item.get("file", "")).replace("\\", "/")
        sequence = sequence_by_key.get((str(item.get("id") or ""), file_name), 0)
        line = int(item.get("original_start_line") or item.get("line") or 0)
        return sequence, line

    ordered = sorted(suggestions, key=undo_order, reverse=True)
    results = []
    for suggestion in ordered:
        results.append(_undo_suggestion_transaction(suggestion, repository_root))
    return {
        "applied": len([item for item in results if item["applied"]]),
        "skipped": len([item for item in results if not item["applied"] and not item.get("failed")]),
        "failed": len([item for item in results if item.get("failed")]),
        "results": results,
    }


def suggestion_status(
    suggestion: dict[str, Any],
    root: Path | None = None,
    *,
    content: str | None = None,
) -> str:
    path = _repo_path(str(suggestion.get("file", "")), root)
    if not path.is_file():
        return "changed"
    if content is None:
        content = _read_text(path)
    try:
        proposed = _locate_proposed_block(suggestion, root, path=path, content=content)
    except AmbiguousBlockError:
        return "changed"
    if proposed is not None:
        if required_edits_present(content, suggestion, path):
            return "applied"
        return "changed"
    try:
        original = _locate_original_block(suggestion, root, path=path, content=content)
    except AmbiguousBlockError:
        return "changed"
    if original is not None:
        return "open"
    return "changed"


def suggestion_statuses(suggestions: list[dict[str, Any]], root: Path | None = None) -> list[dict[str, str]]:
    contents: dict[Path, str | None] = {}
    results = []
    for suggestion in suggestions:
        path = _repo_path(str(suggestion.get("file", "")), root)
        if path not in contents:
            contents[path] = _read_text(path) if path.is_file() else None
        content = contents[path]
        results.append({
            "id": str(suggestion.get("id") or ""),
            "file": str(suggestion.get("file") or ""),
            "status": suggestion_status(suggestion, root, content=content) if content is not None else "changed",
        })
    return results


def print_summary(summary: dict[str, Any], as_json: bool = False) -> None:
    if as_json:
        print(json.dumps(summary, ensure_ascii=False))
        return
    print(f"Applied: {summary['applied']} | Skipped: {summary['skipped']} | Failed: {summary['failed']}")
    for result in summary["results"]:
        status = "APPLIED" if result["applied"] else "FAILED" if result.get("failed") else "SKIPPED"
        print(f"{status} {result['id']} {result['file']} - {result['message']}")


def _short(text: str, limit: int = 80) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else text[:limit - 3] + "..."


def _artifact_context(data: dict[str, Any]) -> dict[str, Any]:
    allowed_keys = (
        "schema_version",
        "run_id",
        "project_key",
        "repository",
        "workspace",
        "pull_request",
        "build_number",
        "head_commit",
        "generated_at",
    )
    return {key: data[key] for key in allowed_keys if data.get(key) not in (None, "")}


def cleanup_undo_state(root: Path | None = None, older_than_days: int = 30) -> dict[str, Any]:
    if older_than_days < 0:
        raise ValueError("older_than_days must be zero or greater")
    cutoff = datetime.now(timezone.utc).timestamp() - older_than_days * 86400
    with _locked_undo_state(root) as (state, state_path):
        kept = []
        removed = []
        for transaction in state["transactions"]:
            status_value = transaction.get("status")
            timestamp_value = transaction.get("undone_at") or transaction.get("completed_at")
            removable = status_value in {"undone", "rolled_back", "manually_reverted"} and isinstance(timestamp_value, str)
            if removable:
                try:
                    timestamp = datetime.fromisoformat(timestamp_value).timestamp()
                except ValueError:
                    timestamp = cutoff + 1
                removable = timestamp <= cutoff
            if removable:
                removed.append(transaction.get("transaction_id"))
            else:
                kept.append(transaction)
        state["transactions"] = kept
        _save_undo_state(state_path, state)
        return {
            "removed": len(removed),
            "transaction_ids": removed,
            "state_path": str(state_path),
        }


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
    summary = apply_suggestions([item], artifact_context=_artifact_context(data))
    print_summary(summary, args.json)
    return 0 if summary["applied"] == 1 else 1


def command_apply_selected(args: argparse.Namespace) -> int:
    data = load_results_file(args.file)
    ids = [value.strip() for value in args.ids.split(",") if value.strip()]
    suggestions = [find_suggestion(data, suggestion_id) for suggestion_id in ids]
    summary = apply_suggestions(suggestions, artifact_context=_artifact_context(data))
    print_summary(summary, args.json)
    return 0 if summary["applied"] > 0 else 1


def command_undo(args: argparse.Namespace) -> int:
    data = load_results_file(args.file)
    item = find_suggestion(data, args.id)
    summary = undo_suggestions([item])
    print_summary(summary, args.json)
    return 0 if summary["applied"] == 1 else 1


def command_undo_selected(args: argparse.Namespace) -> int:
    data = load_results_file(args.file)
    ids = [value.strip() for value in args.ids.split(",") if value.strip()]
    suggestions = [find_suggestion(data, suggestion_id) for suggestion_id in ids]
    summary = undo_suggestions(suggestions)
    print_summary(summary, args.json)
    return 0 if summary["applied"] > 0 else 1


def command_state_clean(args: argparse.Namespace) -> int:
    result = cleanup_undo_state(older_than_days=args.older_than_days)
    if args.json:
        print(json.dumps(result, ensure_ascii=False))
    else:
        print(f"Removed {result['removed']} completed CodeGuardian undo transaction(s).")
    return 0


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
    apply_parser.add_argument("--json", action="store_true", help="Print a machine-readable result")
    apply_parser.set_defaults(func=command_apply)

    selected_parser = subparsers.add_parser("apply-selected", help="Apply selected suggestions")
    selected_parser.add_argument("--file", required=True)
    selected_parser.add_argument("--ids", required=True)
    selected_parser.add_argument("--json", action="store_true", help="Print a machine-readable result")
    selected_parser.set_defaults(func=command_apply_selected)

    undo_parser = subparsers.add_parser("undo", help="Undo one applied suggestion")
    undo_parser.add_argument("--file", required=True)
    undo_parser.add_argument("--id", required=True)
    undo_parser.add_argument("--json", action="store_true", help="Print a machine-readable result")
    undo_parser.set_defaults(func=command_undo)

    undo_selected_parser = subparsers.add_parser("undo-selected", help="Undo selected suggestions")
    undo_selected_parser.add_argument("--file", required=True)
    undo_selected_parser.add_argument("--ids", required=True)
    undo_selected_parser.add_argument("--json", action="store_true", help="Print a machine-readable result")
    undo_selected_parser.set_defaults(func=command_undo_selected)

    validate_parser = subparsers.add_parser("validate", help="Validate one suggestion")
    validate_parser.add_argument("--file", required=True)
    validate_parser.add_argument("--id", required=True)
    validate_parser.set_defaults(func=command_validate)

    status_parser = subparsers.add_parser("status", help="Return suggestion apply status")
    status_parser.add_argument("--file", required=True)
    status_parser.add_argument("--id")
    status_parser.set_defaults(func=command_status)

    clean_parser = subparsers.add_parser("state-clean", help="Remove old completed undo transactions")
    clean_parser.add_argument("--older-than-days", type=int, default=30)
    clean_parser.add_argument("--json", action="store_true", help="Print a machine-readable result")
    clean_parser.set_defaults(func=command_state_clean)

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
