"""Static capability audit for Blackwall implementations.

This is a gate, not a proof. It catches the obvious ways a generated tool can
escape the manifest: imports the manifest did not declare, shell escapes,
dynamic imports, and file writes without a write capability. The subprocess
runner is the second layer.
"""

from __future__ import annotations

import ast
import os
from dataclasses import dataclass
from typing import Any

from .manifest import Capabilities

HIGH = "high"
MEDIUM = "medium"

# Imports that are never allowed in a generated tool.
_BANNED_IMPORTS = {
    "ctypes",
    "cffi",
    "importlib",
    "pickle",
    "marshal",
    "multiprocessing",
    "pty",
    "resource",
    "winreg",
    "_winapi",
}

_NETWORK_IMPORTS = {
    "aiohttp",
    "ftplib",
    "http",
    "httpx",
    "requests",
    "socket",
    "smtplib",
    "ssl",
    "telnetlib",
    "urllib",
    "websockets",
    "xmlrpc",
}

# Pure parsers that cannot open a socket; everything else in the network set
# would need one, and the sandbox has none. Tools fetch through capri_net.
_PURE_NETWORK_IMPORTS = {"urllib.parse", "ipaddress"}


_SCREEN_IMPORTS = {"mss", "PIL", "uiautomation", "winocr", "pyautogui"}

# Calls that can write, create, delete, rename or change permissions on a
# filesystem object. Unknown calls under os/shutil/pathlib/tempfile/io are
# treated as writes too; an allowlist is safer than trying to enumerate every
# new API. The read-only allowlists below keep ordinary observation tools
# usable without write_files.
_WRITE_CALLS = {
    "open",
    "builtins.open",
    "io.open",
    "os.open",
    "os.fdopen",
    "os.write",
    "os.writev",
    "os.pwrite",
    "os.pwritev",
    "os.sendfile",
    "os.copy_file_range",
    "os.makedirs",
    "os.mkdir",
    "os.chmod",
    "os.chown",
    "os.fchmod",
    "os.fchown",
    "os.lchmod",
    "os.lchown",
    "os.chflags",
    "os.utime",
    "os.truncate",
    "os.ftruncate",
    "os.fsync",
    "os.fdatasync",
    "os.remove",
    "os.unlink",
    "os.rmdir",
    "os.removedirs",
    "os.rename",
    "os.renames",
    "os.replace",
    "os.link",
    "os.symlink",
    "os.mknod",
    "os.mkfifo",
    "os.setxattr",
    "os.removexattr",
    "os.dup2",
    "shutil.copy",
    "shutil.copy2",
    "shutil.copyfile",
    "shutil.copyfileobj",
    "shutil.copytree",
    "shutil.move",
    "shutil.rmtree",
    "shutil.copymode",
    "shutil.copystat",
    "shutil.chown",
    "shutil.make_archive",
    "shutil.unpack_archive",
    "pathlib.Path.write_text",
    "pathlib.Path.write_bytes",
    "pathlib.Path.touch",
    "pathlib.Path.mkdir",
    "pathlib.Path.unlink",
    "pathlib.Path.rmdir",
    "pathlib.Path.rename",
    "pathlib.Path.replace",
    "pathlib.Path.chmod",
    "pathlib.Path.lchmod",
    "pathlib.Path.symlink_to",
    "pathlib.Path.hardlink_to",
    "pathlib.Path.link_to",
    "Path.write_text",
    "Path.write_bytes",
    "Path.touch",
    "Path.mkdir",
    "Path.unlink",
    "Path.rmdir",
    "Path.rename",
    "Path.replace",
    "Path.chmod",
    "Path.lchmod",
    "Path.symlink_to",
    "Path.hardlink_to",
    "Path.link_to",
    "tempfile.NamedTemporaryFile",
    "tempfile.TemporaryFile",
    "tempfile.SpooledTemporaryFile",
    "tempfile.mkstemp",
    "tempfile.mkdtemp",
    "tempfile.mktemp",
    "io.FileIO",
    "io.BufferedWriter",
    "io.BufferedRandom",
    "io.BufferedRWPair",
    "io.TextIOWrapper",
    "sqlite3.connect",
    "dbm.open",
    "shelve.open",
    "gzip.open",
    "bz2.open",
    "lzma.open",
    "zipfile.ZipFile",
    "tarfile.open",
    "logging.FileHandler",
    "logging.handlers.FileHandler",
    "logging.handlers.RotatingFileHandler",
    "logging.handlers.TimedRotatingFileHandler",
}

_SAFE_OS_CALLS = {
    "os.access",
    "os.chdir",
    "os.close",
    "os.cpu_count",
    "os.environ.copy",
    "os.environ.get",
    "os.environ.items",
    "os.environ.keys",
    "os.environ.values",
    "os.fspath",
    "os.fsencode",
    "os.fsdecode",
    "os.fstat",
    "os.get_exec_path",
    "os.get_inheritable",
    "os.get_terminal_size",
    "os.getcwd",
    "os.getcwdb",
    "os.getegid",
    "os.getenv",
    "os.getenvb",
    "os.geteuid",
    "os.getgid",
    "os.getloadavg",
    "os.getlogin",
    "os.getpid",
    "os.getppid",
    "os.getpriority",
    "os.getrandom",
    "os.getuid",
    "os.lseek",
    "os.lstat",
    "os.path",
    "os.pipe",
    "os.pread",
    "os.read",
    "os.readlink",
    "os.readv",
    "os.scandir",
    "os.stat",
    "os.statvfs",
    "os.strerror",
    "os.sysconf",
    "os.confstr",
    "os.sched_getaffinity",
    "os.times",
    "os.uname",
    "os.urandom",
    "os.waitstatus_to_exitcode",
    "os.walk",
    "os.fwalk",
}

_SAFE_SHUTIL_CALLS = {
    "shutil.disk_usage",
    "shutil.get_archive_formats",
    "shutil.get_terminal_size",
    "shutil.get_unpack_formats",
    "shutil.which",
}

_SAFE_PATH_METHODS = {
    "absolute",
    "as_posix",
    "as_uri",
    "cwd",
    "Path",
    "PosixPath",
    "PurePath",
    "PurePosixPath",
    "PureWindowsPath",
    "WindowsPath",
    "exists",
    "expanduser",
    "full_match",
    "glob",
    "group",
    "home",
    "is_absolute",
    "is_block_device",
    "is_char_device",
    "is_dir",
    "is_fifo",
    "is_file",
    "is_junction",
    "is_mount",
    "is_relative_to",
    "is_reserved",
    "is_socket",
    "is_symlink",
    "iterdir",
    "joinpath",
    "lstat",
    "match",
    "owner",
    "read_bytes",
    "read_text",
    "readlink",
    "relative_to",
    "resolve",
    "rglob",
    "samefile",
    "stat",
    "walk",
    "with_name",
    "with_segments",
    "with_stem",
    "with_suffix",
}

_SAFE_TEMPFILE_CALLS = {
    "tempfile.gettempdir",
    "tempfile.gettempdirb",
    "tempfile.gettempprefix",
    "tempfile.gettempprefixb",
}

_SAFE_IO_CALLS = {
    "io.BytesIO",
    "io.StringIO",
    "io.open_code",
}

_OPEN_NAMES = {"open", "builtins.open", "io.open"}

_MODULE_PATHS = {
    "builtins",
    "bz2",
    "dbm",
    "gzip",
    "io",
    "logging",
    "logging.handlers",
    "lzma",
    "os",
    "os.path",
    "pathlib",
    "shelve",
    "shutil",
    "sqlite3",
    "tarfile",
    "tempfile",
    "zipfile",
}

# Method names that are unambiguously filesystem writes. This catches a path
# stored in a local (``p = Path(...); p.touch()``) even when no type inference
# was needed. ``replace`` is handled by the path-variable pass because string
# ``replace`` is too common to ban generically.
_PATH_RETURNING_METHODS = {
    "absolute",
    "cwd",
    "expanduser",
    "home",
    "joinpath",
    "parent",
    "parents",
    "readlink",
    "relative_to",
    "resolve",
    "with_name",
    "with_segments",
    "with_stem",
    "with_suffix",
}

# Retrieving an open() is mode-dependent; direct open checks handle calls.
# getattr only escalates the write APIs covered by this gate.
_GETATTR_WRITE_NAMES = {
    "chmod",
    "copyfile",
    "copytree",
    "fdopen",
    "makedirs",
    "touch",
    "write",
}

_GENERIC_WRITE_METHODS = {
    "chmod",
    "hardlink_to",
    "lchmod",
    "link_to",
    "makedirs",
    "mkdir",
    "rename",
    "rmdir",
    "symlink_to",
    "touch",
    "truncate",
    "unlink",
    "write_bytes",
    "write_text",
}
_PATH_PREFIXES = (
    "pathlib.Path.",
    "pathlib.PurePath.",
    "pathlib.PosixPath.",
    "pathlib.WindowsPath.",
    "pathlib.PurePosixPath.",
    "pathlib.PureWindowsPath.",
    "Path.",
    "PurePath.",
    "PosixPath.",
    "WindowsPath.",
)

_BANNED_CALLS = {
    "os.system",
    "os.popen",
    "os.execv",
    "os.execve",
    "os.execvp",
    "os.spawnl",
    "os.spawnle",
    "os.spawnlp",
    "os.spawnv",
    "os.spawnve",
    "os.spawnvp",
    "sys.modules",
}

_BANNED_BUILTINS = {"eval", "exec", "compile", "__import__", "globals", "locals"}


@dataclass(frozen=True)
class Finding:
    severity: str
    code: str
    message: str
    line: int


@dataclass
class AuditReport:
    findings: list[Finding]

    @property
    def ok(self) -> bool:
        return not any(finding.severity == HIGH for finding in self.findings)

    @property
    def high(self) -> list[Finding]:
        return [finding for finding in self.findings if finding.severity == HIGH]

    def payload(self) -> list[dict[str, Any]]:
        return [
            {
                "severity": finding.severity,
                "code": finding.code,
                "message": finding.message,
                "line": finding.line,
            }
            for finding in self.findings
        ]


def _dotted(node: ast.AST) -> str | None:
    """Return ``a.b.c`` for a Name/Attribute chain."""
    parts: list[str] = []
    current: ast.AST | None = node
    while isinstance(current, ast.Attribute):
        parts.append(current.attr)
        current = current.value
    # ``Path("x").write_text(...)`` is an Attribute over a Call. The receiver
    # still tells us which class method is being invoked.
    while isinstance(current, ast.Call):
        current = current.func
        while isinstance(current, ast.Attribute):
            parts.append(current.attr)
            current = current.value
    # ``handles["out"].open(...)`` is an Attribute over a Subscript. Keep the
    # method visible so an arbitrary writer cannot hide behind a container.
    while isinstance(current, ast.Subscript):
        current = current.value
        while isinstance(current, ast.Attribute):
            parts.append(current.attr)
            current = current.value
    if isinstance(current, ast.Name):
        parts.append(current.id)
        return ".".join(reversed(parts))
    return None


def _call_name(node: ast.Call, aliases: dict[str, str]) -> str | None:
    dotted = _dotted(node.func)
    if dotted is None:
        return None
    head, _, tail = dotted.partition(".")
    root = aliases.get(head, head)
    return root + ("." + tail if tail else "")


def _literal_mode(call: ast.Call) -> str | None:
    """The literal open mode when it is statically visible."""
    for keyword in call.keywords:
        if keyword.arg == "mode" and isinstance(keyword.value, ast.Constant):
            value = keyword.value.value
            return value if isinstance(value, str) else None
    if len(call.args) >= 2 and isinstance(call.args[1], ast.Constant):
        value = call.args[1].value
        return value if isinstance(value, str) else None
    return None


def _has_mode_argument(call: ast.Call, *, method: bool = False) -> bool:
    """True when a mode was supplied, even if it is not a literal."""
    if method:
        if call.args:
            return True
        return any(keyword.arg == "mode" for keyword in call.keywords)
    if len(call.args) >= 2:
        return True
    return any(keyword.arg == "mode" for keyword in call.keywords)


def _open_mode_writes(call: ast.Call, *, method: bool = False) -> bool:
    """A write-capable open: literal write mode, or a mode we cannot prove safe."""
    mode = _literal_mode(call)
    if mode is None and method and call.args and isinstance(call.args[0], ast.Constant):
        value = call.args[0].value
        mode = value if isinstance(value, str) else None
    if mode is not None:
        return any(flag in mode for flag in "wax+")
    return _has_mode_argument(call, method=method)


def _flag_value(node: ast.AST) -> int | None:
    """Statically evaluate the simple integer expressions used for os.open flags."""
    if isinstance(node, ast.Constant) and isinstance(node.value, int):
        return node.value
    if isinstance(node, ast.Attribute) and node.attr.startswith("O_"):
        return getattr(os, node.attr, None)
    if isinstance(node, ast.Name) and node.id.startswith("O_"):
        return getattr(os, node.id, None)
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub):
        value = _flag_value(node.operand)
        return -value if value is not None else None
    if isinstance(node, ast.BinOp) and isinstance(node.op, (ast.BitOr, ast.BitAnd)):
        left = _flag_value(node.left)
        right = _flag_value(node.right)
        if left is None or right is None:
            return None
        return left | right if isinstance(node.op, ast.BitOr) else left & right
    return None


def _flags_write(node: ast.AST) -> bool:
    """Classify an os.open flags expression; unknown flags are write-capable."""
    value = _flag_value(node)
    if value is None:
        return True
    access = value & getattr(os, "O_ACCMODE", 3)
    if access != getattr(os, "O_RDONLY", 0):
        return True
    write_flags = 0
    for name in (
        "O_CREAT",
        "O_TRUNC",
        "O_APPEND",
        "O_EXCL",
        "O_TMPFILE",
        "O_TEMPORARY",
        "O_SHORT_LIVED",
    ):
        write_flags |= int(getattr(os, name, 0))
    return bool(value & write_flags)


def _path_method(name: str) -> str | None:
    if name.startswith(_PATH_PREFIXES):
        return name.rsplit(".", 1)[-1]
    return None


def _path_expression(node: ast.AST, aliases: dict[str, str], paths: set[str]) -> bool:
    if isinstance(node, ast.Call):
        name = _call_name(node, aliases) or ""
        if name in {"pathlib.Path", "Path"} or name in paths:
            return True
        head, _, tail = name.partition(".")
        if tail and (head in paths or name.startswith("pathlib.Path.")):
            return tail.rsplit(".", 1)[-1] in _PATH_RETURNING_METHODS
        return False
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
        return _path_expression(node.left, aliases, paths) or _path_expression(
            node.right, aliases, paths
        )
    if isinstance(node, ast.Subscript):
        return _path_expression(node.value, aliases, paths)
    if isinstance(node, ast.Attribute) and node.attr in {"parent", "parents"}:
        return _path_expression(node.value, aliases, paths)
    if isinstance(node, ast.Name):
        return node.id in paths or aliases.get(node.id) in {"pathlib.Path", "Path"}
    dotted = _dotted(node)
    if dotted is not None:
        resolved = aliases.get(dotted.partition(".")[0], dotted.partition(".")[0])
        tail = dotted.partition(".")[2]
        return (resolved + ("." + tail if tail else "")) in {"pathlib.Path", "Path"}
    return False


def _collect_path_vars(tree: ast.AST, aliases: dict[str, str]) -> set[str]:
    paths: set[str] = set()
    for _ in range(4):
        changed = False
        for node in ast.walk(tree):
            if isinstance(node, ast.AnnAssign):
                if not isinstance(node.target, ast.Name) or node.value is None:
                    continue
                if _path_expression(node.value, aliases, paths) and node.target.id not in paths:
                    paths.add(node.target.id)
                    changed = True
                continue
            if not isinstance(node, ast.Assign):
                continue
            if not _path_expression(node.value, aliases, paths):
                continue
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id not in paths:
                    paths.add(target.id)
                    changed = True
        if not changed:
            break
    return paths


def _getattr_requires_write(
    call: ast.Call, aliases: dict[str, str], path_vars: set[str]
) -> bool:
    attribute: ast.AST | None = call.args[1] if len(call.args) >= 2 else None
    for keyword in call.keywords:
        if keyword.arg == "name":
            attribute = keyword.value
    if not call.args:
        return False
    if not isinstance(attribute, ast.Constant) or not isinstance(attribute.value, str):
        # A dynamic attribute is too broad to classify without false positives.
        return False
    if attribute.value not in _GETATTR_WRITE_NAMES:
        return False
    dotted = _dotted(call.args[0])
    if dotted is None:
        return False
    head = dotted.partition(".")[0]
    root = aliases.get(head, head)
    module_root = root.partition(".")[0]
    return root in _MODULE_PATHS or module_root in _MODULE_PATHS or head in path_vars


def _requires_write(
    name: str,
    call: ast.Call,
    path_vars: set[str] | None = None,
) -> bool:
    if name == "os.open":
        flags = call.args[1] if len(call.args) >= 2 else None
        for keyword in call.keywords:
            if keyword.arg == "flags":
                flags = keyword.value
        return True if flags is None else _flags_write(flags)
    if name in _OPEN_NAMES:
        return _open_mode_writes(call)
    if name.endswith(".open"):
        # Path/io/builtins opens are understood above; an arbitrary object's
        # open() is unverifiable and therefore write-capable by default.
        head = name.partition(".")[0]
        if _path_method(name) == "open" or (path_vars and head in path_vars):
            return _open_mode_writes(call, method=True)
        return True
    if name in _WRITE_CALLS:
        return True
    head, _, tail = name.partition(".")
    if path_vars and head in path_vars and tail:
        method = tail.rsplit(".", 1)[-1]
        return method not in _SAFE_PATH_METHODS
    if name.rsplit(".", 1)[-1] in _GENERIC_WRITE_METHODS:
        return True
    if name.startswith("os.path."):
        return False
    if name.startswith("os."):
        return name not in _SAFE_OS_CALLS
    if name.startswith("shutil."):
        return name not in _SAFE_SHUTIL_CALLS
    method = _path_method(name)
    if method is not None:
        return method not in _SAFE_PATH_METHODS
    if name.startswith("tempfile."):
        return name not in _SAFE_TEMPFILE_CALLS
    if name.startswith("io."):
        return name not in _SAFE_IO_CALLS
    return False


def _collect_module_aliases(tree: ast.AST, aliases: dict[str, str]) -> None:
    """Resolve direct assignments of known modules (``m = os``)."""
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        reference = _resolved_reference(node.value, aliases)
        if reference not in _MODULE_PATHS:
            continue
        for target in node.targets:
            if isinstance(target, ast.Name) and target.id not in aliases:
                aliases[target.id] = reference


def _resolved_reference(node: ast.AST, aliases: dict[str, str]) -> str | None:
    dotted = _dotted(node)
    if dotted is None:
        return None
    head, _, tail = dotted.partition(".")
    root = aliases.get(head, head)
    return root + ("." + tail if tail else "")


def _collect_write_aliases(
    tree: ast.AST, aliases: dict[str, str], path_vars: set[str]
) -> dict[str, str]:
    """Map local function aliases to the write API they point at.

    Keeping the canonical name means ``f = open; f(path, "r")`` stays
    read-only, while ``f = open; f(path, mode)`` still requires write_files.
    """
    write_aliases: dict[str, str] = {}
    for _ in range(3):
        changed = False
        for node in ast.walk(tree):
            if isinstance(node, ast.AnnAssign):
                if node.value is None or isinstance(node.value, ast.Call):
                    continue
                targets = [node.target]
                value = node.value
            elif isinstance(node, ast.Assign):
                if isinstance(node.value, ast.Call):
                    # A call result is not a function alias (``p = Path(...)``).
                    # Calls that write are flagged directly by the main audit.
                    continue
                targets = node.targets
                value = node.value
            else:
                continue
            reference = _resolved_reference(value, aliases)
            if reference is None:
                continue
            reference = write_aliases.get(reference, reference)
            probe = ast.Call(func=value, args=[], keywords=[])
            write = reference in _OPEN_NAMES or _requires_write(reference, probe, path_vars)
            for target in targets:
                if isinstance(target, ast.Name) and write_aliases.get(target.id) != reference:
                    write_aliases[target.id] = reference
                    changed = True
        if not changed:
            break
    return write_aliases


def audit_source(source: str, capabilities: Capabilities) -> AuditReport:
    findings: list[Finding] = []
    try:
        tree = ast.parse(source, filename="impl.py")
    except SyntaxError as exc:
        return AuditReport(
            [
                Finding(
                    HIGH,
                    "syntax",
                    f"implementation does not parse: {exc.msg}",
                    int(exc.lineno or 0),
                )
            ]
        )

    aliases: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.split(".")[0]
                if alias.asname:
                    aliases[alias.asname] = alias.name
                else:
                    # ``import os.path`` binds the name ``os``, not
                    # ``os.path.path`` after a later qualified call.
                    aliases[root] = root
                if root in _BANNED_IMPORTS:
                    findings.append(
                        Finding(HIGH, "import.banned", f"import of {alias.name} is not allowed", node.lineno)
                    )
                elif root == "capri_net" and not capabilities.network:
                    findings.append(
                        Finding(
                            HIGH,
                            "import.network",
                            "capri_net needs capabilities.network with declared hosts",
                            node.lineno,
                        )
                    )
                elif root in _NETWORK_IMPORTS and alias.name not in _PURE_NETWORK_IMPORTS:
                    findings.append(
                        Finding(
                            HIGH,
                            "import.network.raw" if capabilities.network else "import.network",
                            f"import of {alias.name} needs a socket and the sandbox has none; "
                            "use `from capri_net import fetch`",
                            node.lineno,
                        )
                    )
                elif root in _SCREEN_IMPORTS and not capabilities.screen:
                    findings.append(
                        Finding(
                            HIGH,
                            "import.screen",
                            f"import of {alias.name} needs capabilities.screen",
                            node.lineno,
                        )
                    )
                elif root == "subprocess" and not capabilities.subprocess:
                    findings.append(
                        Finding(
                            HIGH,
                            "import.subprocess",
                            "import of subprocess needs capabilities.subprocess",
                            node.lineno,
                        )
                    )
        elif isinstance(node, ast.ImportFrom):
            root = (node.module or "").split(".")[0]
            if any(alias.name == "*" for alias in node.names):
                findings.append(
                    Finding(
                        HIGH,
                        "import.star",
                        "star imports cannot be audited; import names explicitly",
                        node.lineno,
                    )
                )
            for alias in node.names:
                if alias.name == "*":
                    continue
                local = alias.asname or alias.name
                aliases[local] = f"{node.module or ''}.{alias.name}".strip(".")
            if root in _BANNED_IMPORTS:
                findings.append(
                    Finding(
                        HIGH,
                        "import.banned",
                        f"import from {node.module} is not allowed",
                        node.lineno,
                    )
                )
            elif root == "capri_net" and not capabilities.network:
                findings.append(
                    Finding(
                        HIGH,
                        "import.network",
                        "capri_net needs capabilities.network with declared hosts",
                        node.lineno,
                    )
                )
            elif (
                root in _NETWORK_IMPORTS
                and (node.module or "") not in _PURE_NETWORK_IMPORTS
                and f"{node.module}.{alias.name}" not in _PURE_NETWORK_IMPORTS
            ):
                findings.append(
                    Finding(
                        HIGH,
                        "import.network.raw" if capabilities.network else "import.network",
                        f"import from {node.module} needs a socket and the sandbox has none; "
                        "use `from capri_net import fetch`",
                        node.lineno,
                    )
                )
            elif root in _SCREEN_IMPORTS and not capabilities.screen:
                findings.append(
                    Finding(
                        HIGH,
                        "import.screen",
                        f"import from {node.module} needs capabilities.screen",
                        node.lineno,
                    )
                )
            elif root == "subprocess" and not capabilities.subprocess:
                findings.append(
                    Finding(
                        HIGH,
                        "import.subprocess",
                        "import from subprocess needs capabilities.subprocess",
                        node.lineno,
                    )
                )

    _collect_module_aliases(tree, aliases)
    path_vars = _collect_path_vars(tree, aliases)
    write_aliases = _collect_write_aliases(tree, aliases, path_vars)

    # Attribute access can be a capability escape even without a call, e.g.
    # ``sys.modules[...] = fake``. Flag the dangerous attribute chains.
    for node in ast.walk(tree):
        if not isinstance(node, ast.Attribute):
            continue
        dotted = _dotted(node)
        if dotted in {"sys.modules", "sys.settrace", "sys.setprofile"}:
            findings.append(
                Finding(HIGH, "call.banned", f"{dotted} is not allowed", node.lineno)
            )

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = _call_name(node, aliases)
        if name is None:
            continue
        policy_name = write_aliases.get(name, name)
        short = name.rsplit(".", 1)[-1]
        if short == "getattr" and not isinstance(node.func, ast.Call):
            if _getattr_requires_write(node, aliases, path_vars) and not capabilities.write_files:
                findings.append(
                    Finding(
                        HIGH,
                        "capability.write",
                        f"{name} may retrieve a file-writing function; "
                        "declare capabilities.write_files",
                        node.lineno,
                    )
                )
            continue
        if short in _BANNED_BUILTINS:
            findings.append(
                Finding(HIGH, "call.banned", f"{name} is not allowed", node.lineno)
            )
            continue
        if name in _BANNED_CALLS or any(name.startswith(prefix + ".") for prefix in _BANNED_CALLS):
            findings.append(
                Finding(HIGH, "call.banned", f"{name} is not allowed", node.lineno)
            )
            continue
        if policy_name != name and (
            policy_name in _BANNED_CALLS
            or any(policy_name.startswith(prefix + ".") for prefix in _BANNED_CALLS)
        ):
            findings.append(
                Finding(HIGH, "call.banned", f"{policy_name} is not allowed", node.lineno)
            )
            continue
        open_call = policy_name in _OPEN_NAMES or policy_name == "os.open" or policy_name.endswith(".open")
        if open_call:
            if _requires_write(policy_name, node, path_vars):
                if not capabilities.write_files:
                    findings.append(
                        Finding(
                            HIGH,
                            "capability.write",
                            f"{policy_name} may write files; declare capabilities.write_files",
                            node.lineno,
                        )
                    )
            else:
                # A literal read-only mode is still a filesystem read.
                if not capabilities.read_files:
                    findings.append(
                        Finding(
                            MEDIUM,
                            "capability.read",
                            f"{policy_name} can read files; declare capabilities.read_files",
                            node.lineno,
                        )
                    )
            continue
        if _requires_write(policy_name, node, path_vars):
            if not capabilities.write_files:
                findings.append(
                    Finding(
                        HIGH,
                        "capability.write",
                        f"{policy_name} may write files; declare capabilities.write_files",
                        node.lineno,
                    )
                )
        elif name.endswith(("read_text", "read_bytes")):
            if not capabilities.read_files:
                findings.append(
                    Finding(
                        MEDIUM,
                        "capability.read",
                        f"{name} reads files; declare capabilities.read_files",
                        node.lineno,
                    )
                )

    return AuditReport(findings)
