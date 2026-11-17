#!/usr/bin/env python3
"""Check Python code against the NASA/JPL "Power of Ten" rules that ruff and
mypy don't already cover.

    python tools/jpl_check.py                # the services, scripts and tools
    python tools/jpl_check.py path/to/file.py some/dir

Rule numbers follow Gerard Holzmann, "The Power of Ten: Rules for Developing
Safety-Critical Code" (NASA/JPL, 2006). Rules checked here:

    JPL1  no recursion: a function may not call itself
    JPL2  every loop has a fixed upper bound: no `while` loops, and no `for`
          over endless iterators (itertools.count, cycle, one-argument repeat)
    JPL4  a function is at most 60 lines long
    JPL6  no `global` or `nonlocal`: keep data in the smallest scope
    JPL8  no eval, exec, compile, __import__ or import_module: no code
          generated or loaded at run time
    JPL9  no getattr, setattr or delattr with a computed attribute name

The other rules are enforced by ruff, mypy and pytest settings in
pyproject.toml; LEARNING.md (lesson 3) maps every rule to its tool.
Test code (any `tests` directory) is exempt. Exit status 1 means violations.
"""

import ast
import sys
from dataclasses import dataclass
from pathlib import Path

MAX_FUNCTION_LINES = 60
MAX_FILES = 10_000  # JPL2 applies to this tool too: bound the file walk
DEFAULT_PATHS = (
    "services/core/app",
    "services/gateway/app",
    "services/agent/app",
    "scripts",
    "tools",
)
DYNAMIC_CODE = frozenset({"eval", "exec", "compile", "__import__", "import_module"})
DYNAMIC_ATTRIBUTES = frozenset({"getattr", "setattr", "delattr"})
ENDLESS_ITERATORS = frozenset({"count", "cycle"})
FUNCTIONS = (ast.FunctionDef, ast.AsyncFunctionDef)


@dataclass(frozen=True)
class Violation:
    path: Path
    line: int
    rule: str
    message: str

    def __str__(self) -> str:
        return f"{self.path}:{self.line}: {self.rule} {self.message}"


def call_name(node: ast.Call) -> str | None:
    """Return "foo" for both `foo(...)` and `obj.foo(...)`."""
    if isinstance(node.func, ast.Name):
        return node.func.id
    if isinstance(node.func, ast.Attribute):
        return node.func.attr
    return None


def calls_itself(func: ast.FunctionDef | ast.AsyncFunctionDef) -> int | None:
    """Line of a direct recursive call (`f()` or `self.f()`), if any."""
    for node in ast.walk(func):
        if not isinstance(node, ast.Call) or call_name(node) != func.name:
            continue
        target = node.func
        on_self = (
            isinstance(target, ast.Attribute)
            and isinstance(target.value, ast.Name)
            and target.value.id in {"self", "cls"}
        )
        if isinstance(target, ast.Name) or on_self:
            return node.lineno
    return None


def check_function(node: ast.AST, path: Path) -> list[Violation]:
    if not isinstance(node, FUNCTIONS):
        return []
    found = []
    length = (node.end_lineno or node.lineno) - node.lineno + 1
    if length > MAX_FUNCTION_LINES:
        found.append(
            Violation(
                path,
                node.lineno,
                "JPL4",
                f"function `{node.name}` is {length} lines (max {MAX_FUNCTION_LINES})",
            )
        )
    line = calls_itself(node)
    if line is not None:
        found.append(Violation(path, line, "JPL1", f"function `{node.name}` calls itself"))
    return found


def is_endless(iterator: ast.expr) -> bool:
    if not isinstance(iterator, ast.Call):
        return False
    name = call_name(iterator)
    if name in ENDLESS_ITERATORS:
        return True
    has_times = len(iterator.args) > 1 or any(k.arg == "times" for k in iterator.keywords)
    return name == "repeat" and not has_times


def check_loop(node: ast.AST, path: Path) -> list[Violation]:
    if isinstance(node, ast.While):
        message = "`while` loop has no fixed upper bound; use `for _ in range(LIMIT)`"
        return [Violation(path, node.lineno, "JPL2", message)]
    if isinstance(node, ast.For | ast.AsyncFor) and is_endless(node.iter):
        return [Violation(path, node.lineno, "JPL2", "loop over an endless iterator")]
    return []


def check_scope(node: ast.AST, path: Path) -> list[Violation]:
    if isinstance(node, ast.Global | ast.Nonlocal):
        keyword = "global" if isinstance(node, ast.Global) else "nonlocal"
        return [Violation(path, node.lineno, "JPL6", f"`{keyword}` widens a variable's scope")]
    return []


def check_call(node: ast.AST, path: Path) -> list[Violation]:
    if not isinstance(node, ast.Call):
        return []
    name = call_name(node)
    if name in DYNAMIC_CODE:
        return [Violation(path, node.lineno, "JPL8", f"`{name}` runs code built at run time")]
    computed = len(node.args) > 1 and not isinstance(node.args[1], ast.Constant)
    if isinstance(node.func, ast.Name) and name in DYNAMIC_ATTRIBUTES and computed:
        return [Violation(path, node.lineno, "JPL9", f"`{name}` with a computed attribute name")]
    return []


CHECKS = (check_function, check_loop, check_scope, check_call)


def check_source(source: str, path: Path) -> list[Violation]:
    tree = ast.parse(source, filename=str(path))
    found = [v for node in ast.walk(tree) for check in CHECKS for v in check(node, path)]
    return sorted(found, key=lambda v: (v.line, v.rule))


def python_files(paths: list[Path]) -> list[Path]:
    files: list[Path] = []
    for path in paths:
        candidates = [path] if path.is_file() else sorted(path.rglob("*.py"))
        files.extend(p for p in candidates if "tests" not in p.parts and p.suffix == ".py")
    if len(files) > MAX_FILES:
        message = f"refusing to check {len(files)} files (limit {MAX_FILES})"
        raise ValueError(message)
    return files


def main(argv: list[str]) -> int:
    files = python_files([Path(arg) for arg in (argv or DEFAULT_PATHS)])
    violations = [v for f in files for v in check_source(f.read_text(encoding="utf-8"), f)]
    for violation in violations:
        print(violation)
    if violations:
        print(f"\n{len(violations)} NASA/JPL rule violation(s).", file=sys.stderr)
        return 1
    print(f"NASA/JPL Power of Ten check passed: {len(files)} files.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
