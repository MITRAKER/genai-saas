import pytest

from app.agent import Snippet
from app.chunking import (
    MAX_CHUNKS_PER_FILE,
    QUERY_CHARS,
    QUERY_FILES,
    SourceFile,
    changed_lines,
    chunk_file,
    decode,
    diff_queries,
    fit,
    is_indexable,
    select_files,
)
from app.github import TreeEntry

FILE = SourceFile("app/main.py", "a" * 40, 100)

# Four files: changed, added (with an added line that starts with "++"),
# deleted, and one whose old version has no newline at its end. Built from
# a list so the blank context line keeps its leading space.
DIFF = "\n".join(
    [
        "diff --git a/app/pricing.py b/app/pricing.py",
        "index 1111111..2222222 100644",
        "--- a/app/pricing.py",
        "+++ b/app/pricing.py",
        "@@ -1,4 +1,5 @@",
        " TAX = 0.2",
        "-def total(price):",
        "+def total(price, discount=0):",
        "+    price -= discount",
        "     return price * (1 + TAX)",
        " ",
        "diff --git a/app/new.py b/app/new.py",
        "new file mode 100644",
        "--- /dev/null",
        "+++ b/app/new.py",
        "@@ -0,0 +1,2 @@",
        "+++counter",
        "+x = 1",
        "diff --git a/app/old.py b/app/old.py",
        "deleted file mode 100644",
        "--- a/app/old.py",
        "+++ /dev/null",
        "@@ -1 +0,0 @@",
        "-gone = True",
        "diff --git a/app/tweak.py b/app/tweak.py",
        "--- a/app/tweak.py",
        "+++ b/app/tweak.py",
        "@@ -1 +1 @@",
        "-a = 1",
        "\\ No newline at end of file",
        "+a = 2",
        "",
    ]
)


def entry(path: str, size: int | None = 10, kind: str = "blob") -> TreeEntry:
    return TreeEntry(path=path, type=kind, sha="b" * 40, size=size)


def numbered_lines(count: int) -> bytes:
    return "".join(f"line {n}\n" for n in range(1, count + 1)).encode()


@pytest.mark.parametrize(
    ("path", "indexed"),
    [
        ("app/main.py", True),
        ("web/App.TSX", True),
        ("deploy/main.tf", True),
        ("README.md", False),
        ("poetry.lock", False),
        ("Makefile", False),
        ("node_modules/left-pad/index.js", False),
        ("services/x/vendor/lib.go", False),
        ("vendor.py", True),  # only folders are skipped by name
    ],
)
def test_which_files_are_indexed(path, indexed):
    assert is_indexable(path) is indexed


def test_select_files_keeps_small_code_files_sorted_by_path():
    entries = [
        entry("b.py"),
        entry("a.py"),
        entry("docs/guide.md"),
        entry("big.py", size=2_000),
        entry("empty.py", size=0),
        entry("no_size.py", size=None),
        entry("src", kind="tree", size=None),
        entry("lib", kind="commit", size=None),
    ]
    files, candidates = select_files(entries, max_files=10, max_bytes=1_000)
    assert [file.path for file in files] == ["a.py", "b.py"]
    assert candidates == 2


def test_select_files_stops_at_max_files_but_counts_them_all():
    files, candidates = select_files([entry(f"f{i}.py") for i in range(5)], 3, 100)
    assert [file.path for file in files] == ["f0.py", "f1.py", "f2.py"]
    assert candidates == 5


def test_decode_refuses_binary_and_invalid_utf8():
    assert decode(b"x = 1\n") == "x = 1\n"
    assert decode(b"\x89PNG\r\n\x1a\n\x00\x00") is None
    assert decode(b"caf\xe9 = 1") is None


def test_a_small_file_is_one_chunk():
    [chunk] = chunk_file(FILE, numbered_lines(5))
    assert (chunk.start_line, chunk.end_line) == (1, 5)
    assert chunk.content == "line 1\nline 2\nline 3\nline 4\nline 5"
    assert chunk.embedding_text() == "app/main.py\n" + chunk.content
    assert chunk.blob_sha == FILE.blob_sha


def test_chunks_overlap_so_code_at_an_edge_appears_whole():
    spans = [(c.start_line, c.end_line) for c in chunk_file(FILE, numbered_lines(130))]
    assert spans == [(1, 60), (51, 110), (101, 130)]


def test_chunk_boundaries():
    assert len(chunk_file(FILE, numbered_lines(60))) == 1
    spans = [(c.start_line, c.end_line) for c in chunk_file(FILE, numbered_lines(61))]
    assert spans == [(1, 60), (51, 61)]


def test_a_long_file_stops_at_max_chunks():
    assert len(chunk_file(FILE, numbered_lines(10_000))) == MAX_CHUNKS_PER_FILE


@pytest.mark.parametrize(
    "data",
    [
        b"x" * 2_000,  # a single very long line: minified or generated
        b"\x00\x01\x02",  # binary
        b"\n\n   \n",  # only blank lines
        b"",
    ],
)
def test_files_with_nothing_to_search_have_no_chunks(data):
    assert chunk_file(FILE, data) == []


def test_changed_lines_per_file():
    assert changed_lines(DIFF) == {
        "app/pricing.py": [
            "def total(price):",
            "def total(price, discount=0):",
            "    price -= discount",
        ],
        "app/new.py": ["++counter", "x = 1"],
        "app/old.py": ["gone = True"],
        "app/tweak.py": ["a = 1", "a = 2"],
    }


def test_text_that_is_not_a_diff_changes_nothing():
    assert changed_lines("") == {}
    assert changed_lines("Binary files a/logo.png and b/logo.png differ\n") == {}


def test_diff_queries_put_the_most_changed_files_first():
    queries = diff_queries(DIFF)
    assert [query.split("\n")[0] for query in queries] == [
        "app/pricing.py",
        "app/new.py",
        "app/tweak.py",
        "app/old.py",
    ]
    assert queries[1] == "app/new.py\n++counter\nx = 1"


def test_diff_queries_are_bounded():
    files = [
        f"--- a/f{i}.py\n+++ b/f{i}.py\n@@ -1 +1 @@\n-{'x' * 5_000}\n+y = {i}\n" for i in range(15)
    ]
    queries = diff_queries("".join(files))
    assert len(queries) == QUERY_FILES
    assert all(len(query) <= len("f00.py\n") + QUERY_CHARS for query in queries)


def snippet(path: str, chars: int) -> Snippet:
    return Snippet(path, 1, 1, "x" * chars)


def test_fit_respects_both_limits_and_keeps_the_order():
    snippets = [snippet("a", 10), snippet("b", 50), snippet("c", 10), snippet("d", 10)]
    assert [s.path for s in fit(snippets, max_count=3, max_chars=30)] == ["a", "c", "d"]
    assert [s.path for s in fit(snippets, max_count=2, max_chars=1_000)] == ["a", "b"]
    assert fit(snippets, max_count=0, max_chars=1_000) == []
