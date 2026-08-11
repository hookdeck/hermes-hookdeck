#!/usr/bin/env python3
"""Check that Hermes still provides everything this plugin borrows from it.

The plugin lives outside the Hermes tree and its tests run against
``tests/hermes_stub.py``, which is the only way to exercise the ingest path
without a Hermes checkout. The cost of that is a blind spot: the stub cannot
notice when the real ``WebhookAdapter`` renames a method the adapter overrides
or calls, and the first symptom would be a gateway that fails to start.

So this reads the upstream source and asserts each borrowed name is still
there. It parses rather than imports — importing Hermes would mean installing
its whole dependency tree to answer a question about names.

It is a smoke alarm, not a type checker. A method that keeps its name and
changes its signature or semantics still gets through, which is why the README
points at a real end-to-end run as the thing that actually proves integration.

    python scripts/check_upstream_contract.py path/to/hermes-agent
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

#: module path -> names the plugin imports or overrides from it.
#:
#: Sources, in order: the `from gateway...` imports at the top of adapter.py,
#: the attributes hermes_stub.py reproduces, and the lazy `agent.skill_commands`
#: import in `_apply_skills`.
CONTRACT: dict[str, dict[str, list[str]]] = {
    "gateway/config.py": {
        "module": ["Platform", "PlatformConfig"],
    },
    "gateway/platforms/base.py": {
        "module": [
            "BasePlatformAdapter",
            "MessageEvent",
            "MessageType",
            "ProcessingOutcome",
            "SendResult",
            "SessionSource",
        ],
        # Inherited and called by HookdeckAdapter, or driven by its tests.
        "BasePlatformAdapter": [
            "build_source",
            "handle_message",
            "on_processing_complete",
            "_mark_connected",
            "_mark_disconnected",
        ],
    },
    "gateway/platforms/webhook.py": {
        "module": ["WebhookAdapter"],
        "WebhookAdapter": [
            # Overridden.
            "on_processing_complete",
            # Called from the delivery path; renaming any of these breaks
            # ingest without breaking startup, which is the worse failure.
            "_render_prompt",
            "_render_delivery_extra",
            "_direct_deliver",
            "_prune_delivery_info",
        ],
    },
    "agent/skill_commands.py": {
        "module": ["build_skill_invocation_message", "get_skill_commands"],
    },
}

#: Attributes the adapter reads or writes on `self` that the base classes own.
#: Assigned in `__init__` upstream, so they are found by scanning assignments
#: rather than definitions.
INHERITED_ATTRIBUTES: dict[str, list[str]] = {
    "gateway/platforms/webhook.py": [
        "_route_processor",
        "_delivery_info",
        "_delivery_info_created",
        "_delivery_info_order",
    ],
    "gateway/platforms/base.py": [
        "_background_tasks",
    ],
}


def _module_level_names(tree: ast.Module) -> set[str]:
    names: set[str] = set()
    for node in tree.body:
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            names.update(t.id for t in node.targets if isinstance(t, ast.Name))
        elif isinstance(node, ast.ImportFrom):
            names.update(a.asname or a.name for a in node.names)
    return names


def _class_member_names(tree: ast.Module, class_name: str) -> set[str]:
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == class_name:
            return {
                child.name
                for child in node.body
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef))
            }
    return set()


def _self_assigned_names(tree: ast.Module) -> set[str]:
    """Every ``self.x = ...`` in the file, which is where base classes declare state."""
    names: set[str] = set()
    for node in ast.walk(tree):
        targets: list[ast.expr] = []
        if isinstance(node, ast.Assign):
            targets = list(node.targets)
        elif isinstance(node, ast.AnnAssign):
            targets = [node.target]
        for target in targets:
            if (
                isinstance(target, ast.Attribute)
                and isinstance(target.value, ast.Name)
                and target.value.id == "self"
            ):
                names.add(target.attr)
    return names


def check(root: Path) -> list[str]:
    problems: list[str] = []

    for rel, expectations in CONTRACT.items():
        path = root / rel
        if not path.exists():
            problems.append(f"{rel}: module is gone")
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))

        for scope, expected in expectations.items():
            found = (
                _module_level_names(tree)
                if scope == "module"
                else _class_member_names(tree, scope)
            )
            if scope != "module" and not found:
                problems.append(f"{rel}: class {scope} is gone")
                continue
            for name in expected:
                if name not in found:
                    where = rel if scope == "module" else f"{rel}:{scope}"
                    problems.append(f"{where}: {name} is gone")

    for rel, attributes in INHERITED_ATTRIBUTES.items():
        path = root / rel
        if not path.exists():
            continue  # already reported above
        assigned = _self_assigned_names(ast.parse(path.read_text(encoding="utf-8")))
        for attribute in attributes:
            if attribute not in assigned:
                problems.append(f"{rel}: self.{attribute} is no longer assigned")

    return problems


def main() -> int:
    if len(sys.argv) != 2:
        print(__doc__)
        return 2
    root = Path(sys.argv[1])
    if not (root / "gateway").is_dir():
        print(f"! {root} does not look like a hermes-agent checkout")
        return 2

    problems = check(root)
    if not problems:
        print(f"✓ every name this plugin borrows is still in {root}")
        return 0

    print(f"✗ {len(problems)} name(s) this plugin depends on have moved:\n")
    for problem in problems:
        print(f"    {problem}")
    print(
        "\nUpdate the adapter and tests/hermes_stub.py together — the stub"
        "\nmatching upstream is the only thing making the test suite mean"
        "\nanything."
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
