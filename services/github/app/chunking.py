"""Cutting a repository into searchable pieces, and a diff into search queries.

Only pure functions here, with no network or database, so they are quick to
test and easy to reason about.
"""

import re
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Final

from .agent import Snippet
from .github import TreeEntry

# Source code and configuration. Documentation, data and lock files are left
# out: they would crowd out the code a reviewer needs.
INDEXED_SUFFIXES: Final = frozenset(
    {
        ".py", ".pyi", ".js", ".jsx", ".mjs", ".ts", ".tsx", ".go", ".rs", ".java",
        ".kt", ".kts", ".scala", ".rb", ".php", ".cs", ".swift", ".c", ".h", ".cc",
        ".cpp", ".hpp", ".m", ".sh", ".sql", ".toml", ".yaml", ".yml", ".tf",
    }
)  # fmt: skip
# Code that is vendored, generated or built rather than written.
SKIPPED_FOLDERS: Final = frozenset(
    {"node_modules", "vendor", "third_party", "dist", "build", "target", ".venv", "venv"}
)
CHUNK_LINES: Final = 60
CHUNK_OVERLAP: Final = 10  # so code at a chunk's edge also appears whole in the next
MAX_CHUNKS_PER_FILE: Final = 40
MAX_LINE_CHARS: Final = 1_000  # longer lines mean minified or generated code
BINARY_SNIFF_BYTES: Final = 8_000
QUERY_FILES: Final = 10  # search for the 10 most changed files of a diff
QUERY_CHARS: Final = 4_000
HUNK_HEADER: Final = re.compile(r"^@@ -\d+(?:,(\d+))? \+\d+(?:,(\d+))? @@")


@dataclass(frozen=True)
class SourceFile:
    path: str
    blob_sha: str
    size: int


@dataclass(frozen=True)
class Chunk:
    path: str
    blob_sha: str
    start_line: int
    end_line: int
    content: str

    def embedding_text(self) -> str:
        # The path is part of the meaning: "billing/invoice.py" says a lot.
        return f"{self.path}\n{self.content}"


def is_indexable(path: str) -> bool:
    parts = PurePosixPath(path)
    return parts.suffix.lower() in INDEXED_SUFFIXES and not SKIPPED_FOLDERS.intersection(
        parts.parts[:-1]
    )


def select_files(
    entries: Iterable[TreeEntry], max_files: int, max_bytes: int
) -> tuple[list[SourceFile], int]:
    """The files worth indexing, at most max_files of them, and how many there were."""
    candidates = sorted(
        (
            SourceFile(entry.path, entry.sha, entry.size)
            for entry in entries
            if entry.type == "blob"
            and entry.size is not None
            and 0 < entry.size <= max_bytes
            and is_indexable(entry.path)
        ),
        key=lambda file: file.path,
    )
    return candidates[:max_files], len(candidates)


def decode(data: bytes) -> str | None:
    """The file as text, or None if it looks binary."""
    if b"\0" in data[:BINARY_SNIFF_BYTES]:
        return None
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return None


def chunk_file(file: SourceFile, data: bytes) -> list[Chunk]:
    """Overlapping windows of CHUNK_LINES lines. Simple, and it works for every
    language; splitting at function boundaries would need a parser per language."""
    text = decode(data)
    if text is None:
        return []
    lines = text.splitlines()
    if any(len(line) > MAX_LINE_CHARS for line in lines):
        return []
    step = CHUNK_LINES - CHUNK_OVERLAP
    chunks: list[Chunk] = []
    for start in range(0, max(len(lines) - CHUNK_OVERLAP, 1), step)[:MAX_CHUNKS_PER_FILE]:
        window = lines[start : start + CHUNK_LINES]
        content = "\n".join(window)
        if content.strip():
            chunks.append(Chunk(file.path, file.blob_sha, start + 1, start + len(window), content))
    return chunks


def changed_lines(diff: str) -> dict[str, list[str]]:
    """{path: added and removed lines} from a unified diff.

    Hunk headers ("@@ -10,7 +10,8 @@") say how many old and new lines follow.
    Counting them is how we know whether "+++ x" starts a new file or is an
    added line that happens to begin with "++".
    """
    files: dict[str, list[str]] = {}
    current: list[str] = []
    old_path, old_left, new_left = "", 0, 0
    for line in diff.splitlines():
        tag = line[:1]
        if old_left > 0 or new_left > 0:
            if tag == "\\":  # "\ No newline at end of file"
                continue
            # A context line (" ") is in both versions, "-" only in the old one
            # and "+" only in the new one. (True counts as 1.)
            old_left -= tag != "+"
            new_left -= tag != "-"
            if tag in {"+", "-"}:
                current.append(line[1:])
        elif line.startswith("--- "):
            old_path = line[4:].removeprefix("a/")
        elif line.startswith("+++ "):
            new_path = line[4:].removeprefix("b/")
            # A deleted file's new path is /dev/null; its old path still finds
            # the code that used it.
            current = files.setdefault(old_path if new_path == "/dev/null" else new_path, [])
        elif header := HUNK_HEADER.match(line):
            old_left, new_left = int(header[1] or 1), int(header[2] or 1)
    return files


def diff_queries(diff: str) -> list[str]:
    """One search text per changed file: its path and its changed lines.
    The files with the most changes come first."""
    files = sorted(changed_lines(diff).items(), key=lambda item: -len(item[1]))
    return [
        f"{path}\n" + "\n".join(lines)[:QUERY_CHARS] for path, lines in files[:QUERY_FILES] if lines
    ]


def fit(snippets: Iterable[Snippet], max_count: int, max_chars: int) -> list[Snippet]:
    """The first snippets that together stay within both limits."""
    chosen: list[Snippet] = []
    used = 0
    for snippet in snippets:
        if len(chosen) == max_count:
            break
        if used + len(snippet.content) <= max_chars:
            chosen.append(snippet)
            used += len(snippet.content)
    return chosen
