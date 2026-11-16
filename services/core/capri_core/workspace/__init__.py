"""Project context: the open workspace, its tree, and its detected commands."""

from .detect import (
    CONVENTIONS_LIMIT,
    confident_commands,
    conventions,
    inspect_project,
    render_inspection,
)
from .service import Workspace, WorkspaceError, WorkspaceService
from .tree import (
    EXCLUDED_DIRS,
    MAX_TREE_DEPTH,
    MAX_TREE_ENTRIES,
    gitignore_rules,
    is_ignored,
    is_link,
    render_tree,
    walk_tree,
)

__all__ = [
    "CONVENTIONS_LIMIT",
    "EXCLUDED_DIRS",
    "MAX_TREE_DEPTH",
    "MAX_TREE_ENTRIES",
    "Workspace",
    "WorkspaceError",
    "WorkspaceService",
    "confident_commands",
    "conventions",
    "gitignore_rules",
    "inspect_project",
    "is_ignored",
    "is_link",
    "render_inspection",
    "render_tree",
    "walk_tree",
]
