"""Project context and trust: workspace state, paths, detection, policy.

No model, no network. Every project is a folder under a temp directory and the
shard lives under a temp ``CAPRI_HOME``.

    uv run python services/core/tests/test_work.py
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

from capri_core import pathsafe
from capri_core.config import CapriConfig, PolicyCfg
from capri_core.memory import repo, retrieval
from capri_core.memory.writer import _in_scope
from capri_core.server.session import Session
from capri_core.store import db
from capri_core.tools import Policy, ToolContext
from capri_core.tools.builtin import fs, project, shell
from capri_core.workspace import detect, tree
from capri_core.workspace.service import WorkspaceError, WorkspaceService

FAILURES: list[str] = []
_CONNS: list[Any] = []


def check(condition: bool, label: str) -> None:
    if condition:
        print(f"  ok   {label}")
    else:
        print(f"  FAIL {label}")
        FAILURES.append(label)


def open_test_shard() -> Any:
    conn = db.open_shard("test")
    _CONNS.append(conn)
    return conn


def make_config(**policy: Any) -> CapriConfig:
    config = CapriConfig()
    config.policy = PolicyCfg(**policy)
    return config


def same(left: str, right: str | Path) -> bool:
    return os.path.normcase(os.path.realpath(left)) == os.path.normcase(
        os.path.realpath(str(right))
    )


def make_junction(link: Path, target: Path) -> bool:
    """A directory junction needs no privilege on Windows.

    Elsewhere a symlink stands in, though some hosts refuse it. Either way the
    caller skips the escape check rather than failing the suite.
    """
    if os.name != "nt":
        try:
            os.symlink(target, link, target_is_directory=True)
        except OSError:
            return False
        return link.exists()
    try:
        done = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(target)],
            capture_output=True,
            text=True,
            timeout=20,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return done.returncode == 0 and link.exists()


# --------------------------------------------------------------------------- lifecycle


def test_lifecycle(config: CapriConfig, root: Path) -> None:
    conn = open_test_shard()
    service = WorkspaceService(conn, config)
    check(service.current() is None, "no project is open before one is chosen")

    workspace = service.open(str(root))
    check(same(workspace.root, root), "open canonicalizes the root")
    check(service.current() is not None, "current() reports the open project")
    check(service.current()["id"] == workspace.id, "current() carries the row id")

    recent = service.recent()
    check(bool(recent) and recent[0]["active"] is True, "recent() marks the active project")

    conn.close()
    reopened = open_test_shard()
    restored = WorkspaceService(reopened, config).restore()
    check(restored is not None and restored.id == workspace.id, "a restart reopens the same project")
    check(
        restored is not None and same(restored.root, root),
        "the restored root is the one that was opened",
    )

    service2 = WorkspaceService(reopened, config)
    service2.restore()
    check(service2.close() is True, "close() releases the project")
    check(service2.current() is None, "nothing is open after close()")
    check(
        WorkspaceService(reopened, config).restore() is None,
        "a closed project does not come back on restart",
    )


def test_persisted_trust(root: Path) -> None:
    project_dir = root / "trusted"
    project_dir.mkdir()
    trusted = make_config(trust_persisted_workspace=True)
    untrusted = make_config(trust_persisted_workspace=False)
    conn = open_test_shard()
    WorkspaceService(conn, trusted).open(str(project_dir))

    reopened = open_test_shard()
    check(
        WorkspaceService(reopened, trusted).restore().trusted is True,
        "a persisted project keeps write trust by default",
    )
    careful = WorkspaceService(open_test_shard(), untrusted).restore()
    check(
        careful is not None and careful.trusted is False,
        "trust_persisted_workspace=false reopens read-only",
    )
    WorkspaceService(reopened, trusted).close()


def test_busy_switch(config: CapriConfig, root: Path) -> None:
    project_dir = root / "busy"
    project_dir.mkdir()
    busy = {"on": True}
    service = WorkspaceService(open_test_shard(), config, busy=lambda: busy["on"])
    code = ""
    try:
        service.open(str(project_dir))
    except WorkspaceError as exc:
        code = exc.code
    check(code == "busy", "a project cannot be switched while a turn is running")

    busy["on"] = False
    workspace = service.open(str(project_dir))
    check(workspace is not None, "the switch works once the turn has finished")
    service.close()


def test_deny_precedence(root: Path) -> None:
    secret = root / "denied"
    secret.mkdir()
    config = make_config(allow_paths=[str(root)], deny_paths=[str(secret)])
    service = WorkspaceService(open_test_shard(), config)
    code = ""
    try:
        service.open(str(secret))
    except WorkspaceError as exc:
        code = exc.code
    check(code == "blocked", "a deny path blocks the project even inside an allow root")

    missing = WorkspaceService(open_test_shard(), config)
    code = ""
    try:
        missing.open(str(root / "no-such-folder"))
    except WorkspaceError as exc:
        code = exc.code
    check(code == "invalid_args", "opening a missing folder is refused")


# --------------------------------------------------------------------------- paths


def test_junction_escape(root: Path) -> None:
    outside = root / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("do not read me", encoding="utf-8")
    workspace = root / "junction-project"
    workspace.mkdir()
    link = workspace / "jump"
    if not make_junction(link, outside):
        print("  skip junction escape: this host will not create a junction or symlink")
        return

    config = make_config(allow_paths=[str(workspace)], deny_paths=[])
    service = WorkspaceService(open_test_shard(), config)
    service.open(str(workspace))

    code = ""
    try:
        service.path_for("jump/secret.txt")
    except WorkspaceError as exc:
        code = exc.code
    check(code == "blocked", "a junction cannot move an existing file out of the project")

    code = ""
    try:
        service.path_for("jump/new-file.txt")
    except WorkspaceError as exc:
        code = exc.code
    check(code == "blocked", "a new file under a junction is refused before it exists")

    policy = Policy(config.policy, str(workspace))
    policy.set_workspace(str(workspace), active=True)
    check(
        not policy.in_workspace(str(link / "secret.txt")),
        "policy does not treat the junction target as inside the project",
    )
    service.close()


def test_tree(root: Path) -> None:
    workspace = root / "tree-project"
    for name in (".git", "node_modules", "dist", "src", "docs"):
        (workspace / name).mkdir(parents=True)
    (workspace / ".git" / "HEAD").write_text("ref", encoding="utf-8")
    (workspace / "node_modules" / "dep.js").write_text("x", encoding="utf-8")
    (workspace / "dist" / "bundle.js").write_text("x", encoding="utf-8")
    (workspace / "src" / "a.py").write_text("print(1)\n", encoding="utf-8")
    (workspace / "README.md").write_text("# x\n", encoding="utf-8")
    (workspace / ".gitignore").write_text("*.log\n", encoding="utf-8")
    (workspace / "noise.log").write_text("x", encoding="utf-8")

    shallow = tree.walk_tree(workspace, depth=1)
    names = {entry["name"] for entry in shallow["entries"]}
    check("src" in names and "README.md" in names, "a shallow walk lists direct children")
    check(
        not ({".git", "node_modules", "dist"} & names),
        "build output and .git never appear in the tree",
    )
    check("noise.log" not in names, "the root .gitignore is honoured")

    deep = tree.walk_tree(workspace, depth=3)
    paths = {entry["path"] for entry in deep["entries"]}
    check("src/a.py" in paths, "a deeper walk reaches nested files")

    capped = tree.walk_tree(workspace, depth=6, max_entries=4)
    check(capped["truncated"] is True, "the tree reports truncation at the entry cap")
    check(len(capped["entries"]) == 4, "the tree stops at max_entries")


def test_tree_refuses_outside(root: Path) -> None:
    workspace = root / "tree-root"
    workspace.mkdir()
    refused = False
    try:
        tree.walk_tree(workspace, start=str(root / "outside"), depth=1)
    except ValueError:
        refused = True
    check(refused, "a tree walk cannot start outside the project root")


# --------------------------------------------------------------------------- detection


def test_detection(root: Path) -> None:
    npm = root / "npm-app"
    npm.mkdir()
    (npm / "package.json").write_text(
        json.dumps({"scripts": {"test": "node --test", "lint": "eslint ."}}),
        encoding="utf-8",
    )
    info = detect.inspect_project(npm)
    commands = {item["command"] for item in info["commands"]}
    check("npm test" in commands, "package.json test script is detected")
    check("npm run lint" in commands, "package.json lint script is detected")
    check("package.json" in info["manifests"], "the manifest is reported")

    placeholder = root / "npm-placeholder"
    placeholder.mkdir()
    (placeholder / "package.json").write_text(
        json.dumps({"scripts": {"test": 'echo "Error: no test specified" && exit 1'}}),
        encoding="utf-8",
    )
    check(
        detect.confident_commands(detect.inspect_project(placeholder)) == [],
        "a placeholder test script is not a detected command",
    )

    python = root / "py-app"
    python.mkdir()
    (python / "pyproject.toml").write_text(
        "[project]\n"
        'name = "demo"\n'
        'dependencies = ["pytest>=8", "ruff"]\n'
        "\n"
        "[tool.pytest.ini_options]\n"
        'testpaths = ["tests"]\n',
        encoding="utf-8",
    )
    py_commands = {item["command"] for item in detect.inspect_project(python)["commands"]}
    check("python -m pytest" in py_commands, "pytest is detected from pyproject.toml")
    check("python -m ruff check ." in py_commands, "ruff is detected from its dependency")

    cargo = root / "rs-app"
    cargo.mkdir()
    (cargo / "Cargo.toml").write_text(
        '[package]\nname = "x"\nversion = "0.1.0"\n', encoding="utf-8"
    )
    check(
        "cargo test" in {item["command"] for item in detect.inspect_project(cargo)["commands"]},
        "cargo test is detected",
    )

    go = root / "go-app"
    go.mkdir()
    (go / "go.mod").write_text("module example.com/x\n\ngo 1.22\n", encoding="utf-8")
    check(
        "go test ./..." in {item["command"] for item in detect.inspect_project(go)["commands"]},
        "go test is detected",
    )

    make = root / "make-app"
    make.mkdir()
    (make / "Makefile").write_text("test:\n\techo hi\n\nlint:\n\techo lint\n", encoding="utf-8")
    make_commands = {item["command"] for item in detect.inspect_project(make)["commands"]}
    check("make test" in make_commands, "a real Makefile test target is detected")
    check("make lint" in make_commands, "a real Makefile lint target is detected")

    empty = root / "empty-app"
    empty.mkdir()
    check(
        detect.inspect_project(empty)["commands"] == [],
        "a project with no manifest gets no commands",
    )

    conventions = empty / ".capri" / "conventions.md"
    conventions.parent.mkdir()
    conventions.write_text("x" * 2500, encoding="utf-8")
    text = detect.inspect_project(empty)["conventions"]
    check(len(text) <= detect.CONVENTIONS_LIMIT + 40, "conventions are capped near 2 KB")
    check(text.endswith("[conventions truncated]"), "a long conventions file is marked truncated")


# --------------------------------------------------------------------------- policy


def test_workspace_policy(root: Path) -> None:
    workspace = root / "policy-project"
    workspace.mkdir()
    (workspace / "app.py").write_text("print(1)\n", encoding="utf-8")

    allowed = make_config(allow_paths=[str(root)], deny_paths=[], workspace_edits="allow")
    policy = Policy(allowed.policy, str(workspace))
    policy.set_workspace(str(workspace), active=True, trusted=True)
    check(
        policy.check(fs.WRITE_SPEC, {"path": "app.py", "content": "x"}).verdict == "allow",
        "a write inside a trusted workspace runs without a prompt",
    )

    policy.set_workspace(str(workspace), active=True, trusted=False)
    check(
        policy.check(fs.WRITE_SPEC, {"path": "app.py", "content": "x"}).verdict == "confirm",
        "a project restored read-only asks before a write",
    )

    careful = make_config(allow_paths=[str(root)], deny_paths=[], workspace_edits="confirm")
    confirm_policy = Policy(careful.policy, str(workspace))
    confirm_policy.set_workspace(str(workspace), active=True, trusted=True)
    check(
        confirm_policy.check(fs.WRITE_SPEC, {"path": "app.py", "content": "x"}).verdict
        == "confirm",
        "workspace_edits=confirm restores a prompt per write",
    )

    outside = root / "outside.txt"
    outside.write_text("x", encoding="utf-8")
    check(
        confirm_policy.check(fs.WRITE_SPEC, {"path": str(outside), "content": "x"}).verdict
        == "confirm",
        "overwriting a file outside the workspace asks",
    )
    new_in_allow = root / "brand-new.txt"
    check(
        confirm_policy.check(fs.WRITE_SPEC, {"path": str(new_in_allow), "content": "x"}).verdict
        == "allow",
        "a new file inside an allowlisted root is not destructive",
    )

    denied_cfg = make_config(allow_paths=[str(root)], deny_paths=[str(root / "secrets")])
    secret_dir = root / "secrets"
    secret_dir.mkdir()
    secret = secret_dir / "key.txt"
    secret.write_text("x", encoding="utf-8")
    deny_policy = Policy(denied_cfg.policy, str(workspace))
    deny_policy.set_workspace(str(workspace), active=True, trusted=True)
    check(
        deny_policy.check(fs.READ_SPEC, {"path": str(secret)}).verdict == "block",
        "the deny list wins over workspace trust",
    )
    check(
        deny_policy.check(fs.WRITE_SPEC, {"path": str(secret), "content": "x"}).verdict
        == "block",
        "the deny list wins for writes too",
    )


def test_policy_defaults() -> None:
    cfg = PolicyCfg()
    check(
        all(path.startswith(str(Path.home())) for path in cfg.allow_paths),
        "the allow roots follow this user's home, not a baked-in one",
    )
    check(
        pathsafe.denied(str(Path.home() / ".ssh" / "id_rsa"), cfg.deny_paths),
        "the default deny list covers this user's SSH keys",
    )
    check(
        str(Path.home() / "projects") in cfg.allow_paths,
        "the default allow list starts at this user's projects",
    )
    if os.name == "nt":
        appdata = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
        check(
            pathsafe.denied(str(Path(appdata) / "Capri" / "token"), cfg.deny_paths),
            "the default deny list covers roaming appdata",
        )
        system_root = os.environ.get("SystemRoot") or "C:/Windows"
        check(
            pathsafe.denied(
                str(Path(system_root) / "System32" / "drivers" / "etc" / "hosts"),
                cfg.deny_paths,
            ),
            "the default deny list covers the Windows directory",
        )


def test_shell_policy(root: Path) -> None:
    workspace = root / "shell-project"
    workspace.mkdir()
    config = make_config(allow_paths=[str(root)], deny_paths=[])
    policy = Policy(config.policy, str(workspace))
    policy.set_workspace(str(workspace), active=True, trusted=True)

    check(
        policy.check(shell.RUN_SPEC, {"command": "git diff --format=%H"}).verdict != "block",
        "git diff --format is not mistaken for the format command",
    )
    check(
        policy.check(shell.RUN_SPEC, {"command": "dotnet format"}).verdict != "block",
        "dotnet format is not blocked by an unanchored fragment",
    )
    check(
        policy.check(shell.RUN_SPEC, {"command": "format C:"}).verdict == "block",
        "a bare format command is blocked",
    )
    check(
        policy.check(shell.RUN_SPEC, {"command": "cmd /c format C:"}).verdict == "block",
        "cmd /c cannot be used to reach a blocked command",
    )
    check(
        policy.check(shell.RUN_SPEC, {"command": "git status && del /s *"}).verdict == "block",
        "one blocked segment blocks the whole command line",
    )
    check(
        policy.check(
            shell.RUN_SPEC, {"command": "echo hi & del /s /q C:/tmp/x"}
        ).verdict
        == "block",
        "a single & cannot chain a blocked command",
    )
    check(
        policy.check(
            shell.RUN_SPEC, {"command": "del /s /q C:/tmp/x & echo done"}
        ).verdict
        == "block",
        "a blocked command before a single & is still blocked",
    )
    check(
        policy.check(shell.RUN_SPEC, {"command": "echo hi 2>&1"}).verdict == "allow",
        "fd duplication is not a command separator",
    )
    check(
        policy.check(shell.RUN_SPEC, {"command": "dir >nul 2>&1"}).verdict == "allow",
        "a discard redirect keeps an inspection read-only",
    )
    check(
        policy.check(
            shell.RUN_SPEC,
            {"command": "echo key >> C:/Users/someone/.ssh/authorized_keys"},
        ).verdict
        == "confirm",
        "a redirect cannot hide a write behind a benign prefix",
    )
    check(
        policy.check(
            shell.RUN_SPEC,
            {"command": "type C:/Users/someone/.ssh/id_rsa > leaked.txt"},
        ).verdict
        == "confirm",
        "redirecting a read out to a file asks first",
    )
    check(
        policy.check(shell.RUN_SPEC, {"command": "git branch -D main"}).verdict == "confirm",
        "git branch -D is not read-only",
    )
    check(
        policy.check(shell.RUN_SPEC, {"command": "git branch -d main"}).verdict == "confirm",
        "git branch -d is not read-only",
    )
    check(
        policy.check(shell.RUN_SPEC, {"command": "git branch feat/topic"}).verdict
        == "confirm",
        "creating a branch is not read-only",
    )
    check(
        policy.check(shell.RUN_SPEC, {"command": "git branch"}).verdict == "allow",
        "listing branches is read-only",
    )
    check(
        policy.check(shell.RUN_SPEC, {"command": "git branch --show-current"}).verdict
        == "allow",
        "git branch --show-current stays benign",
    )
    check(
        policy.check(shell.RUN_SPEC, {"command": "git diff --output=C:/tmp/x.patch"}).verdict
        == "confirm",
        "git diff --output writes a file",
    )
    check(
        policy.check(shell.RUN_SPEC, {"command": "git diff --output C:/tmp/x.patch"}).verdict
        == "confirm",
        "git diff --output with a separate value writes a file",
    )
    check(
        policy.check(shell.RUN_SPEC, {"command": "rg --pre calc pattern ."}).verdict
        == "confirm",
        "rg --pre runs a program",
    )
    check(
        policy.check(shell.RUN_SPEC, {"command": "rg --pretty pattern ."}).verdict == "allow",
        "rg --pretty is still a plain search",
    )

    policy.set_detected_commands(str(workspace), ["dotnet format --verify-no-changes"])
    check(
        policy.check(
            shell.RUN_SPEC,
            {"command": "dotnet format --verify-no-changes", "cwd": str(workspace)},
        ).verdict
        == "allow",
        "a detected lint command runs without a prompt",
    )

    policy.set_detected_commands(str(workspace), ["format C:"])
    check(
        policy.check(shell.RUN_SPEC, {"command": "format C:", "cwd": str(workspace)}).verdict
        == "block",
        "the detected allowlist cannot override the blocklist",
    )

    policy.clear_detected_commands()
    check(
        policy.check(shell.RUN_SPEC, {"command": "npm test", "cwd": str(workspace)}).verdict
        == "confirm",
        "an undetected command still asks",
    )


def test_shell_decode() -> None:
    check(shell._decode_text("café ✓".encode()) == "café ✓", "UTF-8 output decodes")
    check(shell._decode_text(b"") == "", "empty output is empty")
    check(isinstance(shell._decode_text(b"\xff\xfe\x00"), str), "undecodable bytes never raise")
    if os.name == "nt":
        check(
            shell._decode_text("naïve résumé".encode("cp1252")) == "naïve résumé",
            "a cp1252 console decodes to text, not replacement characters",
        )


# --------------------------------------------------------------------------- memory


async def test_memory_scope() -> None:
    conn = open_test_shard()
    repo.insert(
        conn,
        "test",
        kind="fact",
        text="alembic migrations live in this project",
        workspace_id="ws_a",
    )
    repo.insert(conn, "test", kind="fact", text="alembic is a migration tool")

    inside = await retrieval.recall(conn, None, "test", "alembic", workspace_id="ws_a")
    outside = await retrieval.recall(conn, None, "test", "alembic", workspace_id="ws_b")
    chat = await retrieval.recall(conn, None, "test", "alembic", workspace_id="")
    everything = await retrieval.recall(conn, None, "test", "alembic")

    check(len(inside) == 2, "the active project sees its own memory and global memory")
    check(len(outside) == 1, "another project does not see that memory")
    check(len(chat) == 1, "a chat with no project sees global memory only")
    check(len(everything) == 2, "an unscoped read (the memory browser) sees everything")

    check(_in_scope({"workspace_id": None}, "ws_a"), "a global row is in scope everywhere")
    check(_in_scope({"workspace_id": "ws_a"}, "ws_a"), "a row is in scope in its own project")
    check(
        not _in_scope({"workspace_id": "ws_b"}, "ws_a"),
        "a row from another project is out of scope",
    )
    check(_in_scope({"workspace_id": "ws_b"}, None), "an unscoped writer accepts any row")


# --------------------------------------------------------------------------- tools


async def test_project_tools(config: CapriConfig, root: Path) -> None:
    workspace = root / "tool-project"
    workspace.mkdir()
    (workspace / "package.json").write_text(
        json.dumps({"scripts": {"test": "node --test"}}), encoding="utf-8"
    )
    (workspace / "src").mkdir()
    (workspace / "src" / "index.js").write_text("export {}\n", encoding="utf-8")

    conn = open_test_shard()
    policy = Policy(config.policy, str(workspace))
    ctx = ToolContext(
        conn=conn,
        turn_id="trn_work",
        subject="test",
        workspace=str(workspace),
        policy=policy,
        project=None,
    )
    result = await project.project_inspect(ctx)
    check(not result.ok and "no project" in result.output, "project.inspect refuses with no project")
    result = await project.project_tree(ctx)
    check(not result.ok, "project.tree refuses with no project")

    service = WorkspaceService(conn, config)
    service.open(str(workspace))
    ctx.project = service
    result = await project.project_inspect(ctx)
    check(result.ok and "Project:" in result.output, "project.inspect summarises the project")
    check(
        policy.check(
            shell.RUN_SPEC, {"command": "npm test", "cwd": str(workspace)}
        ).verdict
        == "allow",
        "project.inspect feeds the detected command allowlist",
    )

    listing = await project.project_tree(ctx, depth=2)
    check(listing.ok and bool(listing.data["entries"]), "project.tree returns entries")
    paths = {entry["path"] for entry in listing.data["entries"]}
    check("src/index.js" in paths, "project.tree includes nested files")

    escape = await project.project_tree(ctx, path="../outside", depth=1)
    check(not escape.ok, "project.tree refuses a path outside the project")
    service.close()


# --------------------------------------------------------------------------- main


async def main() -> int:
    raw = Path(tempfile.mkdtemp(prefix="capri-work-"))
    try:
        root = raw / "projects"
        root.mkdir()
        os.environ["CAPRI_HOME"] = str(raw / "home")
        config = make_config(allow_paths=[str(root)], deny_paths=[])

        print("lifecycle")
        test_lifecycle(config, root)
        print("persisted trust")
        test_persisted_trust(root)
        print("busy switch")
        test_busy_switch(config, root)
        print("deny precedence")
        test_deny_precedence(root)
        print("paths")
        test_junction_escape(root)
        print("tree")
        test_tree(root)
        test_tree_refuses_outside(root)
        print("detection")
        test_detection(root)
        print("policy")
        test_workspace_policy(root)
        test_policy_defaults()
        test_shell_policy(root)
        print("shell decoding")
        test_shell_decode()
        print("memory")
        await test_memory_scope()
        print("tools")
        await test_project_tools(config, root)
        print("session")
        session = Session.create(open_test_shard())
        check(session.episode_id > 0, "a session still opens against the migrated shard")
    finally:
        for conn in _CONNS:
            try:
                conn.close()
            except Exception:
                pass
        shutil.rmtree(raw, ignore_errors=True)

    print()
    if FAILURES:
        print(f"FAILED: {len(FAILURES)} check(s)")
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
