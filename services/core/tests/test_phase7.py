"""Phase 7 regression checks: profiles, idle maintenance and shard bundles.

    uv run python services/core/tests/test_phase7.py
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import time
import zipfile
from pathlib import Path
from typing import Any

from capri_core.agent.prompt import system_prompt
from capri_core.config import CapriConfig, set_value, schema
from capri_core.server.scheduler import IdleScheduler
from capri_core.store import db
from capri_core.store.shard import ShardError, export_shard, import_shard
from capri_core.tools import Policy, builtin_registry

FAILURES: list[str] = []
_CONNS: list[Any] = []


def check(condition: bool, label: str) -> None:
    if condition:
        print(f"  ok   {label}")
    else:
        print(f"  FAIL {label}")
        FAILURES.append(label)


def open_test_shard() -> Any:
    conn = db.open_shard("phase7")
    _CONNS.append(conn)
    return conn


# --------------------------------------------------------------------------- profiles


def test_profiles() -> None:
    config = CapriConfig()
    check("scheduler" in schema()["properties"], "scheduler is in the generated schema")
    check("profile" in schema()["$defs"]["PolicyCfg"]["properties"], "profile stays in schema")

    focus = system_prompt(builtin_registry().specs(), profile="focus")
    silent = system_prompt(builtin_registry().specs(), profile="silent")
    check("Profile: focus" in focus, "focus adds persona guidance")
    check("Profile: silent" in silent, "silent adds persona guidance")
    check(focus != silent, "profiles change the prompt")

    policy = Policy(config.policy, ".")
    focus_spec = builtin_registry().get("screen.focus")
    assert focus_spec
    check(policy.check(focus_spec, {"window_id": 1}).verdict == "allow", "focus allows screen touch")
    updated = set_value(config, "policy.profile", "silent")
    policy.cfg = updated.policy
    check(policy.check(focus_spec, {"window_id": 1}).verdict == "block", "silent blocks screen touch")
    observe_spec = builtin_registry().get("screen.observe")
    assert observe_spec
    check(policy.check(observe_spec, {}).verdict == "allow", "silent still allows observation")


# --------------------------------------------------------------------------- scheduler


class MemoryStub:
    def __init__(self, conn: Any) -> None:
        self.conn = conn
        self.protected: int | None = None

    def maintain(self, *, protect_episode: int | None = None) -> dict[str, Any]:
        self.protected = protect_episode
        return {"pruned": [7, 8], "remaining": 12}


def _episode(conn: Any, started: int, ended: int, text: str) -> int:
    cur = conn.execute(
        "INSERT INTO episodes (subject, started_at, ended_at) VALUES (?, ?, ?)",
        ("phase7", started, ended),
    )
    episode_id = int(cur.lastrowid or 0)
    conn.execute(
        "INSERT INTO messages (episode_id, turn_id, role, content, created_at)"
        " VALUES (?, ?, 'user', ?, ?)",
        (episode_id, f"trn_{episode_id}", text, started),
    )
    conn.execute(
        "INSERT INTO messages (episode_id, turn_id, role, content, created_at)"
        " VALUES (?, ?, 'assistant', ?, ?)",
        (episode_id, f"trn_{episode_id}", "noted", ended),
    )
    return episode_id


def test_scheduler() -> None:
    conn = open_test_shard()
    now = time.time()
    recent = _episode(conn, int((now - 3600) * 1000), int((now - 3500) * 1000), "recent work")
    old = _episode(
        conn,
        int((now - 200 * 86_400) * 1000),
        int((now - 199 * 86_400) * 1000),
        "ancient work",
    )
    conn.execute(
        "INSERT INTO traces (turn_id, span, name, payload, created_at) VALUES"
        " ('stale', 'tool', 'old.trace', '{}', ?)",
        (int((now - 100 * 86_400) * 1000),),
    )
    conn.execute(
        "INSERT INTO traces (turn_id, span, name, payload, created_at) VALUES"
        " ('fresh', 'tool', 'fresh.trace', '{}', ?)",
        (int((now - 1 * 86_400) * 1000),),
    )

    config = CapriConfig()
    memory = MemoryStub(conn)
    seen: list[dict[str, Any]] = []
    scheduler = IdleScheduler(
        config,
        conn,
        "phase7",
        memory,
        lambda: set(),
        clock=lambda: now,
        on_result=seen.append,
    )
    scheduler.last_activity = now - 500
    result = scheduler.run_once()
    check(result["skipped"] is False, "idle scheduler runs when the room is quiet")
    check(result["journaled"] == 2, "both completed episodes get a journal")
    check(
        bool(seen) and seen[-1].get("journaled") == 2,
        "a pass that ran announces its counts",
    )
    check(memory.protected is None, "memory maintenance protects no active episode")
    check(memory.maintain()["remaining"] == 12, "memory maintenance is delegated")

    row = conn.execute("SELECT summary, journal FROM episodes WHERE id = ?", (recent,)).fetchone()
    check(bool(row and row["summary"]), "journal has a summary")
    journal = json.loads(row["journal"]) if row and row["journal"] else {}
    check(journal.get("version") == 1 and journal.get("turns") == 1, "journal has a version and turns")
    check("recent work" in (row["summary"] or ""), "summary names the work")

    gone = conn.execute("SELECT 1 FROM episodes WHERE id = ?", (old,)).fetchone()
    check(gone is None, "journaled episode past retention is pruned")
    check(
        conn.execute("SELECT 1 FROM messages WHERE episode_id = ?", (old,)).fetchone() is None,
        "pruning cascades to messages",
    )
    names = {r["name"] for r in conn.execute("SELECT name FROM traces").fetchall()}
    check("old.trace" not in names and "fresh.trace" in names, "old traces are pruned")

    # A running turn protects its episode from both memory decay and deletion.
    active_id = _episode(conn, int((now - 60) * 1000), int((now - 50) * 1000), "active")
    scheduler.active_episodes = lambda: {active_id}
    scheduler.run_once(force=True)
    check(memory.protected == active_id, "active episode is protected from decay")
    still = conn.execute("SELECT 1 FROM episodes WHERE id = ?", (active_id,)).fetchone()
    check(still is not None, "active episode is not pruned")

    scheduler.active_episodes = lambda: set()
    scheduler.last_activity = now
    announced = len(seen)
    skipped = scheduler.run_once()
    check(skipped["skipped"] is True and skipped["reason"] == "not idle yet", "recent activity defers")
    check(len(seen) == announced, "a deferred pass announces nothing")

    scheduler.active_episodes = lambda: {active_id}
    scheduler.active_turns = lambda: 1
    scheduler.last_activity = now - 500
    skipped = scheduler.run_once()
    check(skipped["skipped"] is True and skipped["reason"] == "active turn", "a running turn defers")


# --------------------------------------------------------------------------- shard


def test_shard_bundle(tmp: Path) -> None:
    conn = open_test_shard()
    conn.execute(
        "INSERT INTO memories (subject, kind, text, importance, confidence, created_at,"
        " accessed_at) VALUES ('phase7', 'fact', 'keep this', 0.9, 1.0, ?, ?)",
        (int(time.time() * 1000), int(time.time() * 1000)),
    )
    config = CapriConfig()
    target = tmp / "bundle.capri"
    exported = export_shard(conn, target, config=config, subject="phase7")
    manifest = exported["manifest"]
    check(target.exists(), "bundle was written")
    check(manifest["format"] == "capri-shard" and manifest["formatVersion"] == 1, "manifest format")
    check(manifest["embedDim"] == config.models.embed.dim, "manifest records embedding dimension")
    check(manifest["counts"]["memories"] == 1, "manifest records counts")

    conn.execute("UPDATE memories SET text = 'mutated'")
    imported = import_shard(conn, target, config=config, subject="phase7")
    row = conn.execute("SELECT text FROM memories WHERE subject = 'phase7'").fetchone()
    check(row is not None and row["text"] == "keep this", "import restores the snapshot")
    check(imported["counts"]["memories"] == 1, "import reports restored counts")

    wrong_dim = CapriConfig()
    wrong_dim.models.embed.dim = 512
    try:
        import_shard(conn, target, config=wrong_dim, subject="phase7")
    except ShardError as exc:
        check("dim" in str(exc).lower(), "import refuses a dimension mismatch")
    else:
        check(False, "import refuses a dimension mismatch")

    try:
        import_shard(conn, target, config=config, subject="someone-else")
    except ShardError as exc:
        check("subject" in str(exc).lower(), "import refuses a foreign subject")
    else:
        check(False, "import refuses a foreign subject")

    bad = tmp / "bad-version.capri"
    with zipfile.ZipFile(target) as source, zipfile.ZipFile(bad, "w") as dest:
        altered = dict(manifest)
        altered["formatVersion"] = 99
        dest.writestr("manifest.json", json.dumps(altered))
        dest.writestr("shard.db", source.read("shard.db"))
    try:
        import_shard(conn, bad, config=config, subject="phase7")
    except ShardError as exc:
        check("format" in str(exc).lower(), "import refuses a future bundle format")
    else:
        check(False, "import refuses a future bundle format")

    row = conn.execute("SELECT text FROM memories WHERE subject = 'phase7'").fetchone()
    check(row is not None and row["text"] == "keep this", "rejected imports leave the shard alone")


def main() -> int:
    raw = Path(tempfile.mkdtemp(prefix="capri-phase7-"))
    try:
        os.environ["CAPRI_HOME"] = str(raw / "home")
        print("profiles")
        test_profiles()
        print("scheduler")
        test_scheduler()
        print("shard bundle")
        test_shard_bundle(raw)
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
    sys.exit(main())
