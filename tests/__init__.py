"""Makes `tests` an importable package.

test_analysis.py and test_report.py import from `tests.conftest` and
`tests.test_differential` directly. Without this file, pytest can only
resolve those as a package by accident — when the interpreter happens to
put the repo root on `sys.path` first, which `python -m pytest` does and a
bare `pytest` invocation does not. CLAUDE.md documents the bare form
(`pytest tests/ -x -q`), and that's what CI runs, so this needs to work
without relying on which way it's invoked.
"""
