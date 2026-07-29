"""Root-confined local workspace tools."""

from __future__ import annotations

from itertools import islice
from pathlib import Path

from base_agent.tools import FunctionTool, tool


def workspace_tools(
    root: str | Path,
    *,
    max_read_bytes: int = 1_000_000,
    max_write_bytes: int = 1_000_000,
    max_entries: int = 200,
    max_matches: int = 200,
    max_search_files: int = 1_000,
    max_match_characters: int = 2_000,
) -> tuple[FunctionTool, ...]:
    """Build bounded filesystem tools confined to one local workspace root."""
    workspace_root = Path(root).expanduser().resolve(strict=True)
    if not workspace_root.is_dir():
        raise ValueError("workspace root must be a directory")
    for name, value in (
        ("max_read_bytes", max_read_bytes),
        ("max_write_bytes", max_write_bytes),
        ("max_entries", max_entries),
        ("max_matches", max_matches),
        ("max_search_files", max_search_files),
        ("max_match_characters", max_match_characters),
    ):
        if value < 1:
            raise ValueError(f"{name} must be greater than zero")

    @tool(name="workspace_list", permissions=frozenset({"workspace:read"}))
    def list_entries(
        path: str = ".",
        recursive: bool = False,
    ) -> dict[str, object]:
        """List bounded file and directory paths inside the configured workspace."""
        directory = _resolve_existing(workspace_root, path)
        if not directory.is_dir():
            raise ValueError(f"not a directory: {path}")
        iterator = directory.rglob("*") if recursive else directory.iterdir()
        entries: list[dict[str, object]] = []
        truncated = False
        candidates = list(islice(iterator, max_entries + 1))
        if len(candidates) > max_entries:
            truncated = True
        for candidate in sorted(candidates[:max_entries]):
            candidate = candidate.resolve(strict=True)
            _ensure_within_root(workspace_root, candidate)
            entries.append(
                {
                    "path": candidate.relative_to(workspace_root).as_posix(),
                    "type": "directory" if candidate.is_dir() else "file",
                    "size_bytes": candidate.stat().st_size if candidate.is_file() else None,
                }
            )
        return {"entries": entries, "truncated": truncated}

    @tool(name="workspace_read_text", permissions=frozenset({"workspace:read"}))
    def read_text(path: str) -> dict[str, object]:
        """Read a bounded UTF-8 text file inside the configured workspace."""
        target = _resolve_existing(workspace_root, path)
        if not target.is_file():
            raise ValueError(f"not a file: {path}")
        size = target.stat().st_size
        if size > max_read_bytes:
            raise ValueError(f"file exceeds {max_read_bytes} bytes")
        try:
            content = target.read_text(encoding="utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError(f"file is not valid UTF-8 text: {path}") from exc
        return {
            "path": target.relative_to(workspace_root).as_posix(),
            "content": content,
            "size_bytes": size,
        }

    @tool(name="workspace_write_text", permissions=frozenset({"workspace:write"}))
    def write_text(
        path: str,
        content: str,
        append: bool = False,
        create_parents: bool = False,
    ) -> dict[str, object]:
        """Write bounded UTF-8 text inside the configured workspace."""
        encoded = content.encode("utf-8")
        if len(encoded) > max_write_bytes:
            raise ValueError(f"content exceeds {max_write_bytes} UTF-8 bytes")
        target = _resolve_for_write(workspace_root, path)
        if target.exists() and not target.is_file():
            raise ValueError(f"not a file: {path}")
        existing_size = target.stat().st_size if target.exists() and append else 0
        if existing_size + len(encoded) > max_write_bytes:
            raise ValueError(
                f"resulting file exceeds {max_write_bytes} UTF-8 bytes"
            )
        if not target.parent.exists():
            if not create_parents:
                raise ValueError("parent directory does not exist")
            target.parent.mkdir(parents=True)
        mode = "a" if append else "w"
        with target.open(mode, encoding="utf-8") as stream:
            stream.write(content)
        return {
            "path": target.relative_to(workspace_root).as_posix(),
            "bytes_written": len(encoded),
            "append": append,
        }

    @tool(name="workspace_search_text", permissions=frozenset({"workspace:read"}))
    def search_text(
        query: str,
        path: str = ".",
        file_pattern: str = "*",
        case_sensitive: bool = False,
    ) -> dict[str, object]:
        """Search for literal text in bounded UTF-8 workspace files."""
        if not query:
            raise ValueError("query must not be empty")
        pattern_path = Path(file_pattern)
        if pattern_path.is_absolute() or ".." in pattern_path.parts:
            raise ValueError("file_pattern must stay inside the search directory")
        directory = _resolve_existing(workspace_root, path)
        if not directory.is_dir():
            raise ValueError(f"not a directory: {path}")
        needle = query if case_sensitive else query.casefold()
        matches: list[dict[str, object]] = []
        truncated = False
        candidates = list(islice(directory.rglob(file_pattern), max_search_files + 1))
        if len(candidates) > max_search_files:
            truncated = True
        for candidate in sorted(candidates[:max_search_files]):
            candidate = candidate.resolve(strict=True)
            _ensure_within_root(workspace_root, candidate)
            if not candidate.is_file() or candidate.stat().st_size > max_read_bytes:
                continue
            try:
                lines = candidate.read_text(encoding="utf-8").splitlines()
            except UnicodeDecodeError:
                continue
            for line_number, line in enumerate(lines, start=1):
                haystack = line if case_sensitive else line.casefold()
                if needle not in haystack:
                    continue
                if len(matches) >= max_matches:
                    truncated = True
                    break
                matches.append(
                    {
                        "path": candidate.relative_to(workspace_root).as_posix(),
                        "line": line_number,
                        "text": line[:max_match_characters],
                        "text_truncated": len(line) > max_match_characters,
                    }
                )
            if truncated:
                break
        return {"matches": matches, "truncated": truncated}

    return list_entries, read_text, write_text, search_text


def _resolve_existing(root: Path, path: str) -> Path:
    candidate = (root / path).resolve(strict=False)
    _ensure_within_root(root, candidate)
    return candidate.resolve(strict=True)


def _resolve_for_write(root: Path, path: str) -> Path:
    resolved = (root / path).resolve(strict=False)
    _ensure_within_root(root, resolved)
    return resolved


def _ensure_within_root(root: Path, candidate: Path) -> None:
    if not candidate.is_relative_to(root):
        raise ValueError("path escapes the configured workspace root")
