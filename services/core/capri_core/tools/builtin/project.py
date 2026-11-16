"""Project tools: describe the open project and walk its tree.

Neither tool can open, close or switch a project. Opening a folder is a user
act (the picker in the shell); the model may only look at the one that is
already open. A tool that could choose its own workspace would make the trust
grant meaningless.
"""

from __future__ import annotations

import time

from ...workspace import detect, tree
from ...workspace.service import WorkspaceError
from ..base import ToolContext, ToolResult, ToolSpec, elapsed_ms

INSPECT_SPEC = ToolSpec(
    name="project.inspect",
    description=(
        "Describe the open project: root, manifests, detected test/lint/build "
        "commands, and its .capri/conventions.md."
    ),
    tier=0,
    max_calls_per_turn=4,
    observation="summary",
    params={
        "type": "object",
        "properties": {},
        "required": [],
        "additionalProperties": False,
    },
)

TREE_SPEC = ToolSpec(
    name="project.tree",
    description=(
        "List files in the open project, directories first. Honours .gitignore "
        "and skips build output, symlinks and junctions."
    ),
    tier=0,
    max_calls_per_turn=4,
    observation="tree",
    params={
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "Project-relative folder. Default is the root.",
            },
            "depth": {
                "type": "integer",
                "description": "Levels below path. Default 3, capped at 6.",
            },
        },
        "required": [],
        "additionalProperties": False,
    },
)


def _no_project(name: str) -> ToolResult:
    return ToolResult.error(
        f"{name}: no project is open. Ask the user to open a folder; you cannot "
        "choose one yourself."
    )


async def project_inspect(ctx: ToolContext) -> ToolResult:
    started = time.monotonic()
    service = getattr(ctx, "project", None)
    if service is None or getattr(service, "active", None) is None:
        return _no_project("project.inspect")

    info = service.inspect()
    # Detection feeds two consumers: the planner's summary and the shell
    # allowlist. A confident test command should not need a confirmation prompt
    # on every turn just because it came from a manifest the user wrote.
    if ctx.policy is not None:
        ctx.policy.set_detected_commands(
            str(info.get("root") or ""), detect.confident_commands(info)
        )

    return ToolResult(
        ok=True,
        output=detect.render_inspection(info),
        data={
            "root": info.get("root"),
            "manifests": info.get("manifests") or [],
            "commands": info.get("commands") or [],
        },
        ms=elapsed_ms(started),
    )


async def project_tree(
    ctx: ToolContext, *, path: str = "", depth: int = 3
) -> ToolResult:
    started = time.monotonic()
    service = getattr(ctx, "project", None)
    if service is None or getattr(service, "active", None) is None:
        return _no_project("project.tree")

    try:
        result = service.tree(str(path or ""), int(depth or 3))
    except WorkspaceError as exc:
        return ToolResult.error(f"project.tree: {exc}")
    except (TypeError, ValueError) as exc:
        return ToolResult.error(f"project.tree: {exc}")

    return ToolResult(
        ok=True,
        output=tree.render_tree(result),
        data={
            "root": result.get("root"),
            "path": result.get("path") or "",
            "entries": result.get("entries") or [],
            "truncated": bool(result.get("truncated")),
        },
        ms=elapsed_ms(started),
    )
