"""Static audit of stability invariants (ARCHITECTURE.md §18).

Fails the build on constructs that historically caused hangs:
  * ``while True`` / ``while 1`` (unbounded loops);
  * ``shell=True`` in subprocess calls;
  * ``communicate()`` / ``wait()`` / ``join()`` / ``get()`` without a timeout
    on objects that may block forever;
  * bare ``except:`` and ``except ...: pass`` that silently swallow errors;
  * recursion (a function calling itself) — retries must be iterative.

A line may opt out with the comment ``# invariant-ok: <reason>``.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

PKG = Path(__file__).resolve().parents[2] / "videogen"
FILES = sorted(p for p in PKG.rglob("*.py"))
BLOCKING_METHODS = {"communicate", "wait", "join"}


def _optout(lines: list[str], node: ast.AST) -> bool:
    start = getattr(node, "lineno", 0)
    end = getattr(node, "end_lineno", start) or start
    return any("invariant-ok:" in lines[i - 1] for i in range(start, min(end, len(lines)) + 1))


def _violations(path: Path) -> list[str]:
    src = path.read_text(encoding="utf-8")
    lines = src.splitlines()
    tree = ast.parse(src)
    out: list[str] = []

    def add(node: ast.AST, msg: str) -> None:
        if not _optout(lines, node):
            rel = path.relative_to(PKG.parent) if path.is_relative_to(PKG.parent) else path.name
            out.append(f"{rel}:{node.lineno}: {msg}")

    for node in ast.walk(tree):
        if isinstance(node, ast.While) and isinstance(node.test, ast.Constant) and node.test.value:
            add(node, "unbounded `while True` loop")
        if isinstance(node, ast.Call):
            for kw in node.keywords:
                if kw.arg == "shell" and isinstance(kw.value, ast.Constant) and kw.value.value:
                    add(node, "subprocess with shell=True")
            f = node.func
            if isinstance(f, ast.Attribute) and f.attr in BLOCKING_METHODS:
                has_timeout = any(k.arg == "timeout" for k in node.keywords) or (
                    f.attr in ("wait", "join") and len(node.args) >= 1)
                if not has_timeout:
                    add(node, f"`.{f.attr}()` without timeout")
            if isinstance(f, ast.Attribute) and f.attr == "get" and isinstance(f.value, ast.Name) \
                    and f.value.id.endswith(("queue", "_q", "q")) and not node.args and not node.keywords:
                add(node, "queue `.get()` without timeout")
        if isinstance(node, ast.ExceptHandler):
            if node.type is None:
                add(node, "bare `except:`")
            if len(node.body) == 1 and isinstance(node.body[0], ast.Pass):
                add(node.body[0], "`except: pass` swallows errors silently")
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for sub in ast.walk(node):
                if isinstance(sub, ast.Call) and isinstance(sub.func, ast.Name) and sub.func.id == node.name:
                    add(sub, f"recursive call to {node.name}()")
    return out


@pytest.mark.parametrize("path", FILES, ids=lambda p: str(p.relative_to(PKG)))
def test_stability_invariants(path):
    assert _violations(path) == []


def test_auditor_catches_bad_code(tmp_path):
    bad = tmp_path / "bad_example.py"
    bad.write_text(
        "import subprocess\n"
        "def retry(n):\n"
        "    while True:\n"
        "        try:\n"
        "            subprocess.run(['x'], shell=True)\n"
        "        except:\n"
        "            pass\n"
        "        p.communicate()\n"
        "        t.join()\n"
        "        return retry(n - 1)\n", encoding="utf-8")
    v = _violations(bad)
    text = "\n".join(v)
    for needle in ("while True", "shell=True", "bare `except:`", "except: pass", "communicate",
                   "join", "recursive"):
        assert needle in text, needle
