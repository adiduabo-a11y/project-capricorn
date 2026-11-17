"""Read a pasted Claude-Desktop config as servers Capri can launch.

Pure functions: no config model, no registry, no server. The settings panel and
the ``mcp.register`` tool both read a paste through here, so one paste means one
thing whichever door it came in by.

A Claude config describes more than Capri can run — HTTP servers, ``inputs``
that Claude Desktop prompts for, placeholder expansion. None of that is
silently dropped: a server becomes a row or a skipped entry that says why, and
whatever could not be honoured becomes a note.
"""

from __future__ import annotations

import json
import re
from typing import Any, Mapping

# A name becomes a config key and a tool prefix (``mcp.<name>.<tool>``), which
# the registry and the decision grammar both have to carry.
_KEEP = re.compile(r"[^A-Za-z0-9_-]+")
_PLACEHOLDER = re.compile(r"\$\{([^}]+)\}")
_NAME_MAX = 32

# Every pasted server starts where a third-party server belongs: asking first.
DEFAULT_TIER = 2


class ConfigError(ValueError):
    """The pasted text is not a config this daemon can read.

    Carries the line and column JSON reported, so the panel can point at the
    broken character instead of showing a traceback.
    """

    def __init__(
        self, message: str, *, line: int | None = None, column: int | None = None
    ) -> None:
        self.line = line
        self.column = column
        if line is not None and column is not None:
            message = f"{message} (line {line}, column {column})"
        super().__init__(message)


def key(name: str) -> str:
    """A server name as a config key and a tool prefix.

    A pasted "My Server" is not refused, it becomes ``My_Server``: the part
    that has to survive is the launch command, not the spelling of the label.
    """
    clean = _KEEP.sub("_", str(name or "")).strip("_-")
    clean = clean[:_NAME_MAX].strip("_-")
    return clean or "server"


def parse_config(text: str) -> dict[str, Any]:
    """The servers in a pasted config, plus what was left out and why.

    Returns ``{"servers", "skipped", "notes", "inputs"}``. ``servers`` is ready
    to become ``mcp.servers.<name>``; ``skipped`` is ``[{"name", "reason"}]``
    and is never empty without a reason on every entry.
    """
    data = _load(text)
    servers = data.get("mcpServers")
    if not isinstance(servers, Mapping):
        raise ConfigError('this config has no "mcpServers" object', line=1, column=1)

    notes: list[str] = []
    declared = _input_notes(data, notes)
    rows: list[dict[str, Any]] = []
    skipped: list[dict[str, str]] = []
    used: set[str] = set()
    trusted_by_paste: list[str] = []

    for raw_name, entry in servers.items():
        name = key(raw_name)
        if name in used:
            # Two pasted servers that clean to one key would overwrite each
            # other in config.yaml; the later one gets a suffix, not the door.
            suffix = 2
            while f"{name}_{suffix}" in used:
                suffix += 1
            notes.append(
                f"{str(raw_name)!r} became {name}_{suffix}: {name} was already taken."
            )
            name = f"{name}_{suffix}"
        used.add(name)

        row, reason = _row(name, entry)
        if row is None:
            skipped.append({"name": name, "reason": reason})
        else:
            rows.append(row)
            if isinstance(entry, Mapping) and ("tier" in entry or "enabled" in entry):
                trusted_by_paste.append(name)

    if trusted_by_paste:
        notes.append(
            ", ".join(trusted_by_paste)
            + " came with a trust tier or switch in the paste: ignored. Every "
            f"imported server starts at tier {DEFAULT_TIER}, and its row in "
            "Settings is where it is trusted."
        )

    placeholders = sorted({found for row in rows for found in _placeholders(row)})
    if placeholders:
        notes.append(
            "the config refers to "
            + ", ".join(f"${{{found}}}" for found in placeholders)
            + ": Capri expands nothing, so each server receives that text as written."
        )

    return {"servers": rows, "skipped": skipped, "notes": notes, "inputs": declared}


# --------------------------------------------------------------------------- internals


def _load(text: str) -> dict[str, Any]:
    raw = (text or "").strip()
    if not raw:
        raise ConfigError("nothing to read: paste a config first", line=1, column=1)
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ConfigError(
            f"this is not valid JSON: {exc.msg}", line=exc.lineno, column=exc.colno
        ) from None
    if not isinstance(data, dict):
        raise ConfigError(
            f"a config is a JSON object, not a {type(data).__name__}", line=1, column=1
        )
    return data


def _row(name: str, entry: Any) -> tuple[dict[str, Any] | None, str]:
    """One ``mcpServers`` entry as a config row, or the reason it cannot be one."""
    if not isinstance(entry, Mapping):
        return None, "not an object, so there is nothing to launch"

    command = str(entry.get("command") or "").strip()
    url = str(entry.get("url") or "").strip()
    if command and url:
        return None, (
            "declares both a command and a url — Capri runs stdio servers only, "
            "so keep the one you mean"
        )
    if not command:
        if url:
            return None, f"stdio servers only — this one is a url server ({url})"
        return None, "no command to launch"

    raw_args = entry.get("args")
    if raw_args is None:
        raw_args = []
    if not isinstance(raw_args, list) or not all(isinstance(arg, str) for arg in raw_args):
        return None, "args has to be a list of strings"

    env = _env(entry.get("env"))
    if env is None:
        return None, "env has to be an object of NAME: value pairs"

    # Trust is never read from the paste. A Claude config has no tier field, so
    # one carrying "tier": 0 or "enabled": false was either edited by hand or
    # never written by Claude at all; either way the decision belongs to the
    # person and the panel, not to the text being imported.
    return (
        {
            "name": name,
            "command": command,
            "args": [str(arg) for arg in raw_args],
            "env": env,
            "cwd": str(entry.get("cwd") or "").strip(),
            "tier": DEFAULT_TIER,
            "enabled": True,
        },
        "",
    )


def _env(raw: Any) -> dict[str, str] | None:
    if raw is None:
        return {}
    if not isinstance(raw, Mapping):
        return None
    # A JSON null is an empty value, not the word "None": a server that needs an
    # empty variable should get one.
    return {
        str(name): "" if value is None else str(value) for name, value in raw.items()
    }


def _placeholders(row: dict[str, Any]) -> list[str]:
    seen: list[str] = []
    parts = [row["command"], row["cwd"], *row["args"], *row["env"].values()]
    for part in parts:
        seen.extend(_PLACEHOLDER.findall(str(part)))
    return seen


def _input_notes(data: Mapping[str, Any], notes: list[str]) -> list[str]:
    """What ``inputs`` means for Capri, and every id it declares."""
    if "inputs" not in data:
        return []

    raw = data.get("inputs")
    declared: list[str] = []
    if isinstance(raw, list):
        for entry in raw:
            if isinstance(entry, Mapping):
                name = str(entry.get("id") or entry.get("name") or "").strip()
                if name:
                    declared.append(name)
    elif raw is not None:
        notes.append("inputs was not a list, so it was ignored.")
        return []

    if declared:
        notes.append(
            f"{len(declared)} input{'s' if len(declared) != 1 else ''} declared "
            f"({', '.join(declared)}): Claude Desktop asks for those values when it "
            "starts a server; Capri does not, so the config's own values are what the "
            "server gets."
        )
    else:
        notes.append("no inputs declared: this config asks for no values from you.")
    return declared
