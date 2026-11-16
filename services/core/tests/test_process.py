"""Closing an app: matching, refusals, and what the tool reports.

    uv run python services/core/tests/test_process.py

The process list and taskkill are replaced with fakes, so nothing on the machine
is touched. What is checked is the part that decides: which process a name means,
which ones may never be closed, and whether "closed" is a claim the tool can back
up.
"""

from __future__ import annotations

import asyncio
import sys

from capri_core.config import CapriConfig
from capri_core.tools import builtin_registry
from capri_core.tools.base import ToolContext
from capri_core.tools.builtin import process
from capri_core.tools.policy import Policy

FAILURES: list[str] = []


def check(condition: bool, label: str) -> None:
    if condition:
        print(f"  ok   {label}")
    else:
        print(f"  FAIL {label}")
        FAILURES.append(label)


def ctx() -> ToolContext:
    return ToolContext(conn=None, turn_id="t", subject="default", workspace=".")


class Fake:
    """A process table that taskkill actually edits."""

    def __init__(self, rows: list[tuple[str, int]]) -> None:
        self.rows = list(rows)
        self.killed: list[tuple[int, bool]] = []

    async def tasklist(self) -> list[tuple[str, int]]:
        return list(self.rows)

    async def taskkill(self, pid: int, force: bool) -> None:
        self.killed.append((pid, force))
        self.rows = [row for row in self.rows if row[1] != pid]


def use(fake: Fake, *, settle: float = 0.2) -> None:
    process._tasklist_impl = fake.tasklist
    process._taskkill_impl = fake.taskkill
    process._SETTLE_S = settle


def reset() -> None:
    process._tasklist_impl = None
    process._taskkill_impl = None
    process._SETTLE_S = 4.0


async def main() -> int:
    print("matching")
    check(process.matches("roblox studio", "RobloxStudioBeta.exe"), "spaces and .exe do not matter")
    check(process.matches("STUDIO", "RobloxStudioBeta.exe"), "matching ignores case")
    check(process.matches("chrome", "RobloxStudioBeta.exe") is False, "a different app does not match")
    check(process.matches("", "anything.exe") is False, "an empty query matches nothing")

    print("refusals")
    check(bool(process.refusable("lsass.exe")), "system processes are refused")
    check(bool(process.refusable("Project Capricorn.exe")), "the app she lives in is refused")
    check(bool(process.refusable("capri-core.exe")), "the daemon is refused")
    check(process.refusable("Code.exe") == "", "an ordinary app is allowed")
    found, reason = process.select("studio", [("RobloxStudioBeta.exe", 42)])
    check(found == [("RobloxStudioBeta.exe", 42)] and not reason, "a single match is selected")
    # A query can match an ordinary app and a system process at once; the whole
    # set is refused rather than closing the half that looks harmless.
    found, reason = process.select("ass", [("PassThru.exe", 1), ("lsass.exe", 2)])
    check(found == [] and "Windows" in reason, f"a protected match poisons the set: {reason}")

    print("listing")
    use(Fake([]))
    try:
        fake = Fake([("Code.exe", 1), ("Code.exe", 2), ("chrome.exe", 3)])
        use(fake)
        result = await process.process_list(ctx())
        check("Code.exe (1, 2)" in result.output, f"pids are grouped by name: {result.output!r}")
        check("chrome.exe (3)" in result.output, "every process is listed")
    finally:
        reset()

    print("closing")
    fake = Fake([("RobloxStudioBeta.exe", 42), ("chrome.exe", 3)])
    use(fake)
    try:
        result = await process.process_close(ctx(), name="roblox studio")
        check(result.ok, f"a matching app is closed: {result.output!r}")
        check(fake.killed == [(42, False)], f"it is asked politely, not killed: {fake.killed}")
        result = await process.process_close(ctx(), name="roblox studio")
        check(not result.ok and "matches" in result.output, "closing it twice reports it is gone")
    finally:
        reset()

    fake = Fake([("RobloxStudioBeta.exe", 42), ("RobloxStudioBeta2.exe", 43)])
    use(fake)
    try:
        result = await process.process_close(ctx(), name="studio")
        check(not result.ok and not fake.killed, "two matches refuse without all=true")
        check("all=true" in result.output, f"and say how to proceed: {result.output!r}")
        result = await process.process_close(ctx(), name="studio", all=True)
        check(result.ok and len(fake.killed) == 2, "all=true closes every match")
    finally:
        reset()

    fake = Fake([("RobloxStudioBeta.exe", 42)])
    use(fake)
    try:
        result = await process.process_close(ctx(), name="studio", force=True)
        check(fake.killed == [(42, True)], "force passes through to the kill")
        check("terminated" in result.output, f"and the report says so: {result.output!r}")
    finally:
        reset()

    class Stubborn(Fake):
        async def taskkill(self, pid: int, force: bool) -> None:
            self.killed.append((pid, force))  # the app ignores it, e.g. a save prompt

    fake = Stubborn([("RobloxStudioBeta.exe", 42)])
    use(fake, settle=0.3)
    try:
        result = await process.process_close(ctx(), name="studio")
        check(not result.ok, "a close that did not take is not reported as success")
        check("force=true" in result.output, f"and offers the next step: {result.output!r}")
        check(
            result.data == {"closed": ["RobloxStudioBeta.exe (42)"], "still": ["RobloxStudioBeta.exe (42)"]},
            f"both halves are in the data: {result.data}",
        )
    finally:
        reset()

    fake = Fake([("lsass.exe", 7)])
    use(fake)
    try:
        result = await process.process_close(ctx(), name="lsass")
        check(not result.ok and not fake.killed, "the refusal happens before any kill")
    finally:
        reset()

    print("manifest")
    check(process.CLOSE_SPEC.tier == 2, "closing an app asks first")
    check(process.LIST_SPEC.tier == 0, "listing is free")
    check("process.close" in builtin_registry().names(), "the daemon registers the tool")
    verdict = Policy(CapriConfig().policy, ".").check(process.CLOSE_SPEC, {"name": "chrome"})
    check(verdict.verdict == "confirm", f"policy confirms a close: {verdict.verdict}")

    print()
    if FAILURES:
        print(f"FAILED: {len(FAILURES)} check(s)")
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
