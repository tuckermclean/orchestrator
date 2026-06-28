"""Doctrine Principle 2 — You may not fix what you cannot see (DOCTRINE.md).

A fire-and-forget background task whose exception vanishes is, operationally, a task that
never fails — and never runs. A bare `asyncio.create_task` whose error goes to a logger
nobody watches hides the failure entirely.

This gate statically scans production code under ``src/`` and fails if any
``asyncio.create_task(...)`` (or ``loop.create_task(...)``) result is left without error
egress. Egress means one of:

  * the task handle gets an ``.add_done_callback(...)`` in the same module, or
  * the task is a lifecycle task owned on ``self.<attr>`` (held for the object's
    lifetime, cancelled on shutdown; its coroutine self-handles errors).

A bare ``create_task(...)`` whose handle is discarded is the exact anti-pattern the
doctrine forbids. Test scaffolding (``fakes.py``) is exempt — fire-and-forget there lives
and dies with the test.
"""

from __future__ import annotations

import ast
import pathlib

_SRC = pathlib.Path(__file__).resolve().parents[2] / "src"


def _is_create_task_call(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "create_task"
    )


def _callbacked_names(tree: ast.AST) -> set[str]:
    """Variable names ``x`` for which ``x.add_done_callback(...)`` appears in the module."""
    names: set[str] = set()
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "add_done_callback"
            and isinstance(node.func.value, ast.Name)
        ):
            names.add(node.func.value.id)
    return names


def test_no_fire_and_forget_without_error_egress() -> None:
    offenders: list[str] = []

    for path in sorted(_SRC.rglob("*.py")):
        if path.name == "fakes.py":  # test scaffolding — exempt
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        callbacked = _callbacked_names(tree)
        rel = path.relative_to(_SRC.parent)

        for node in ast.walk(tree):
            # Assigned task: `x = create_task(...)` or `self._x = create_task(...)`
            if isinstance(node, ast.Assign) and _is_create_task_call(node.value):
                target = node.targets[0]
                if isinstance(target, ast.Attribute):
                    # self.<attr> = create_task(...) — lifecycle task, exempt.
                    continue
                if isinstance(target, ast.Name):
                    if target.id not in callbacked:
                        offenders.append(
                            f"{rel}:{node.lineno} — task '{target.id}' = create_task(...) "
                            "has no .add_done_callback (no error egress)"
                        )
                else:
                    offenders.append(
                        f"{rel}:{node.lineno} — create_task(...) assigned to a non-name "
                        "target; cannot verify error egress"
                    )
            # Annotated assign: `task: asyncio.Task = create_task(...)`
            elif (
                isinstance(node, ast.AnnAssign)
                and node.value is not None
                and _is_create_task_call(node.value)
            ):
                if isinstance(node.target, ast.Attribute):
                    continue
                if isinstance(node.target, ast.Name) and node.target.id not in callbacked:
                    offenders.append(
                        f"{rel}:{node.lineno} — task '{node.target.id}' = create_task(...) "
                        "has no .add_done_callback (no error egress)"
                    )
            # Bare, discarded task: `create_task(...)` as a statement.
            elif isinstance(node, ast.Expr) and _is_create_task_call(node.value):
                offenders.append(
                    f"{rel}:{node.lineno} — bare create_task(...) with no handle and no "
                    "error egress"
                )

    assert not offenders, (
        "Doctrine Principle 2 (DOCTRINE.md): fire-and-forget tasks without error egress.\n"
        "Add `.add_done_callback(...)` that surfaces the exception, own the task on "
        "`self.<attr>`, or `await` it.\n  " + "\n  ".join(offenders)
    )
