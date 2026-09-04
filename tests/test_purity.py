"""
Enforces the one rule the whole project rests on: app/domain/ is pure.

If a decision can reach a database, replay breaks, the audit trail becomes a
reconstruction instead of a record, and the benchmark slows to a crawl. This test
is cheap insurance against a helpful refactor.
"""
from __future__ import annotations

import ast
from pathlib import Path

DOMAIN = Path(__file__).resolve().parent.parent / "app" / "domain"

FORBIDDEN_MODULES = {
    "app.repos", "app.services", "app.api", "app.controllers", "app.workers",
    "sim", "sqlalchemy", "requests", "httpx", "fastapi", "openai",
    "google", "google.generativeai", "razorpay", "sqlite3", "random",
}
FORBIDDEN_CALLS = {"now", "utcnow", "today"}   # datetime.now(), etc.


def _domain_files():
    return [p for p in DOMAIN.glob("*.py") if p.name != "__init__.py"]


def test_domain_has_no_impure_imports():
    bad = []
    for path in _domain_files():
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            for n in names:
                root = n.split(".")[0]
                if n in FORBIDDEN_MODULES or root in FORBIDDEN_MODULES:
                    bad.append(f"{path.name}: imports {n}")
    assert not bad, "app/domain must stay pure:\n  " + "\n  ".join(bad)


def test_domain_never_reads_the_clock():
    bad = []
    for path in _domain_files():
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                if node.func.attr in FORBIDDEN_CALLS:
                    obj = getattr(node.func.value, "id", "")
                    if obj in ("datetime", "date", "time"):
                        bad.append(f"{path.name}:{node.lineno}: {obj}.{node.func.attr}()")
    assert not bad, "`now` must arrive on the snapshot, never from a clock:\n  " + "\n  ".join(bad)


def test_every_domain_file_is_importable():
    import importlib
    for path in _domain_files():
        importlib.import_module(f"app.domain.{path.stem}")
