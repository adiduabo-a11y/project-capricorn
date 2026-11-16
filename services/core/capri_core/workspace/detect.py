"""Project detection without executing anything.

The model is told which commands this project uses, and the shell policy gets
an exact-match allowlist for the ones the detector is confident about. Both
come from files, never from model output. A manifest that only implies a
command (a Makefile target called ``deploy``) is not a test command; it stays
out.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any, Iterable

try:  # Python 3.11+
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - the project requires 3.11+
    tomllib = None  # type: ignore[assignment]

CONVENTIONS_PATH = Path(".capri") / "conventions.md"
CONVENTIONS_LIMIT = 2000

_MAKE_TARGET = re.compile(r"^([A-Za-z0-9_.-]+)\s*:(?!=)")
_PLACEHOLDER = re.compile(r"no test specified|error: no test", re.IGNORECASE)
_DEP_SPLIT = re.compile(r"[\[<>=!~;\s]")


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8-sig", errors="replace")
    except OSError:
        return ""


def _toml(path: Path) -> dict[str, Any]:
    if tomllib is None:
        return {}
    try:
        return tomllib.loads(_read(path))
    except (OSError, ValueError):
        return {}


def _command(
    kind: str, command: str, source: str, confident: bool = True
) -> dict[str, Any]:
    return {
        "kind": kind,
        "command": command,
        "source": source,
        "confident": bool(confident),
    }


def _package_manager(root: Path) -> str:
    if (root / "pnpm-lock.yaml").exists():
        return "pnpm"
    if (root / "yarn.lock").exists():
        return "yarn"
    if (root / "bun.lockb").exists() or (root / "bun.lock").exists():
        return "bun"
    return "npm"


def _npm(root: Path, source: str) -> list[dict[str, Any]]:
    try:
        data = json.loads(_read(root / "package.json") or "{}")
    except ValueError:
        return []
    scripts = data.get("scripts") if isinstance(data, dict) else None
    if not isinstance(scripts, dict):
        return []

    manager = _package_manager(root)
    out: list[dict[str, Any]] = []
    for kind, script in (("test", "test"), ("lint", "lint"), ("build", "build")):
        body = str(scripts.get(script) or "").strip()
        if not body or _PLACEHOLDER.search(body):
            continue
        if script == "test":
            command = f"{manager} test" if manager != "npm" else "npm test"
        else:
            command = f"{manager} run {script}"
        out.append(_command(kind, command, source))
    return out


def _dependency_strings(data: dict[str, Any]) -> Iterable[str]:
    project = data.get("project") or {}
    if isinstance(project, dict):
        for item in project.get("dependencies") or []:
            yield str(item)
        optional = project.get("optional-dependencies") or {}
        if isinstance(optional, dict):
            for group in optional.values():
                for item in group or []:
                    yield str(item)
    groups = data.get("dependency-groups") or {}
    if isinstance(groups, dict):
        for group in groups.values():
            for item in group or []:
                yield str(item)
    poetry = (data.get("tool") or {}).get("poetry") or {}
    if isinstance(poetry, dict):
        for name in (poetry.get("dependencies") or {}):
            yield str(name)
        for group in (poetry.get("group") or {}).values():
            if isinstance(group, dict):
                for name in group.get("dependencies") or {}:
                    yield str(name)


def _uses(data: dict[str, Any], package: str) -> bool:
    wanted = package.replace("_", "-").lower()
    for spec in _dependency_strings(data):
        name = _DEP_SPLIT.split(spec.strip().lower(), 1)[0].replace("_", "-")
        if name == wanted:
            return True
    return False


def _python(root: Path, source: str) -> list[dict[str, Any]]:
    if not (root / "pyproject.toml").exists():
        return []
    data = _toml(root / "pyproject.toml")
    tool = data.get("tool") or {}
    if not isinstance(tool, dict):
        tool = {}

    uv = (root / "uv.lock").exists() or "uv" in tool
    pytest_confident = "pytest" in tool or _uses(data, "pytest")
    ruff_confident = "ruff" in tool or _uses(data, "ruff")
    mypy_confident = "mypy" in tool or _uses(data, "mypy")

    out: list[dict[str, Any]] = []
    if pytest_confident:
        command = "uv run pytest" if uv else "python -m pytest"
        out.append(_command("test", command, source))
    if ruff_confident:
        command = "uv run ruff check ." if uv else "python -m ruff check ."
        out.append(_command("lint", command, source))
    if mypy_confident:
        command = "uv run mypy ." if uv else "python -m mypy ."
        out.append(_command("lint", command, source))
    return out


def _cargo(root: Path, source: str) -> list[dict[str, Any]]:
    if not (root / "Cargo.toml").exists():
        return []
    return [
        _command("test", "cargo test", source),
        _command("lint", "cargo clippy -- -D warnings", source),
        _command("build", "cargo build", source),
    ]


def _go(root: Path, source: str) -> list[dict[str, Any]]:
    if not (root / "go.mod").exists():
        return []
    return [
        _command("test", "go test ./...", source),
        _command("lint", "go vet ./...", source),
        _command("build", "go build ./...", source),
    ]


def _dotnet(root: Path) -> tuple[list[str], list[dict[str, Any]]]:
    solutions = sorted(root.glob("*.sln"))
    projects = sorted(root.glob("*.csproj"))
    if not solutions and not projects:
        return [], []
    target = solutions[0] if solutions else projects[0]
    needs_quote = " " in target.name
    quoted = f'"{target.name}"' if needs_quote else target.name
    commands = [
        _command("test", f"dotnet test {quoted}", target.name),
        _command("build", f"dotnet build {quoted}", target.name),
        _command("lint", "dotnet format --verify-no-changes", target.name),
    ]
    return [target.name], commands


def _make(root: Path, source: str) -> list[dict[str, Any]]:
    path = root / "Makefile"
    if not path.exists():
        return []
    targets = []
    for line in _read(path).splitlines():
        match = _MAKE_TARGET.match(line)
        if match:
            name = match.group(1)
            if not name.startswith("."):
                targets.append(name)
    found = set(targets)

    out: list[dict[str, Any]] = []
    if "test" in found or "tests" in found:
        name = "test" if "test" in found else "tests"
        out.append(_command("test", f"make {name}", source))
    if "lint" in found:
        out.append(_command("lint", "make lint", source))
    if "build" in found:
        out.append(_command("build", "make build", source))
    return out


def conventions(root: str | Path) -> str:
    path = Path(root) / CONVENTIONS_PATH
    try:
        text = path.read_text(encoding="utf-8-sig", errors="replace")
    except OSError:
        return ""
    if len(text) > CONVENTIONS_LIMIT:
        return text[:CONVENTIONS_LIMIT] + "\n[conventions truncated]"
    return text.strip()


def inspect_project(root: str | Path) -> dict[str, Any]:
    """Detect manifests, commands and conventions. Reads only."""
    root_path = Path(root)
    manifests: list[str] = []
    commands: list[dict[str, Any]] = []

    for name in ("package.json", "pyproject.toml", "Cargo.toml", "go.mod", "Makefile"):
        if (root_path / name).exists():
            manifests.append(name)

    if (root_path / "package.json").exists():
        commands.extend(_npm(root_path, "package.json"))
    if (root_path / "pyproject.toml").exists():
        commands.extend(_python(root_path, "pyproject.toml"))
    if (root_path / "Cargo.toml").exists():
        commands.extend(_cargo(root_path, "Cargo.toml"))
    if (root_path / "go.mod").exists():
        commands.extend(_go(root_path, "go.mod"))

    dotnet_manifests, dotnet_commands = _dotnet(root_path)
    manifests.extend(dotnet_manifests)
    commands.extend(dotnet_commands)

    if (root_path / "Makefile").exists():
        commands.extend(_make(root_path, "Makefile"))

    # A command is only useful once; a manifest may report the same one twice.
    seen: set[tuple[str, str]] = set()
    unique: list[dict[str, Any]] = []
    for item in commands:
        key = (item["kind"], item["command"])
        if key in seen:
            continue
        seen.add(key)
        unique.append(item)

    return {
        "root": str(root_path),
        "name": root_path.name,
        "manifests": manifests,
        "commands": unique,
        "conventions": conventions(root_path),
    }


def confident_commands(info: dict[str, Any]) -> list[str]:
    return [
        str(item["command"])
        for item in info.get("commands") or []
        if item.get("confident")
    ]


def render_inspection(info: dict[str, Any]) -> str:
    """Summary for the planner, deliberately short."""
    lines = [f"Project: {info.get('name') or info.get('root')}", f"Root: {info.get('root')}"]
    manifests = info.get("manifests") or []
    if manifests:
        lines.append("Manifests: " + ", ".join(str(name) for name in manifests))
    commands = info.get("commands") or []
    if commands:
        lines.append("Commands:")
        for item in commands:
            mark = "" if item.get("confident") else " (candidate)"
            lines.append(f"- {item.get('kind')}: {item.get('command')}{mark}")
    else:
        lines.append("Commands: none detected; ask before assuming one.")
    conventions_text = str(info.get("conventions") or "").strip()
    if conventions_text:
        lines.extend(["", "Conventions:", conventions_text])
    return "\n".join(lines)
