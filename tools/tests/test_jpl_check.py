from pathlib import Path

import pytest

import jpl_check

REPO_ROOT = Path(__file__).resolve().parents[2]


def rules(source: str) -> list[str]:
    return [v.rule for v in jpl_check.check_source(source, Path("sample.py"))]


def test_clean_code_passes():
    source = (
        "def total(items: list[int]) -> int:\n"
        "    result = 0\n"
        "    for item in items[:100]:\n"
        "        result += item\n"
        "    return result\n"
    )
    assert rules(source) == []


@pytest.mark.parametrize(
    ("source", "rule"),
    [
        ("def f(n):\n    return f(n - 1)\n", "JPL1"),
        ("class A:\n    def g(self):\n        return self.g()\n", "JPL1"),
        ("while True:\n    pass\n", "JPL2"),
        ("i = 0\nwhile i < 10:\n    i += 1\n", "JPL2"),
        ("import itertools\nfor i in itertools.count():\n    pass\n", "JPL2"),
        ("from itertools import repeat\nfor x in repeat(1):\n    pass\n", "JPL2"),
        ("x = 0\ndef f():\n    global x\n", "JPL6"),
        ("eval('1 + 1')\n", "JPL8"),
        ("compile(source, 'x.py', 'exec')\n", "JPL8"),
        ("import builtins\nbuiltins.compile(source, 'x.py', 'exec')\n", "JPL8"),
        ("df.eval(expression)\n", "JPL8"),
        ("import importlib\nimportlib.import_module(name)\n", "JPL8"),
        ("getattr(obj, name)\n", "JPL9"),
    ],
)
def test_each_rule_is_detected(source, rule):
    assert rule in rules(source)


def test_long_function_is_detected():
    body = "".join(f"    x{i} = {i}\n" for i in range(jpl_check.MAX_FUNCTION_LINES))
    assert rules("def long():\n" + body) == ["JPL4"]


def test_allowed_lookalikes():
    source = (
        "import itertools\n"
        "for x in itertools.repeat(1, 3):\n"  # bounded repeat
        "    pass\n"
        "getattr(obj, 'name')\n"  # literal attribute name
        "import re\n"
        "WORD = re.compile('[a-z]+')\n"  # a regex, not code
        "class A:\n"
        "    def close(self):\n"
        "        self.client.close()\n"  # same name, different object
    )
    assert rules(source) == []


def test_calls_without_a_plain_name_are_ignored():
    assert rules("handlers[0]()\nmake()()\n") == []


def test_file_walk_is_bounded(tmp_path, monkeypatch):
    (tmp_path / "a.py").write_text("x = 1\n")
    monkeypatch.setattr(jpl_check, "MAX_FILES", 0)
    with pytest.raises(ValueError, match="refusing to check 1 files"):
        jpl_check.python_files([tmp_path])


def test_main_reports_violations(tmp_path, capsys):
    bad = tmp_path / "bad.py"
    bad.write_text("while True:\n    pass\n")
    assert jpl_check.main([str(bad)]) == 1
    assert "JPL2" in capsys.readouterr().out


def test_tests_directories_are_exempt(tmp_path):
    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()
    (tests_dir / "test_x.py").write_text("while True:\n    pass\n")
    assert jpl_check.python_files([tmp_path]) == []


def test_this_repository_follows_the_rules(monkeypatch, capsys):
    monkeypatch.chdir(REPO_ROOT)
    assert jpl_check.main([]) == 0, capsys.readouterr().out
