"""What she is told she can do, against what she can actually do.

    uv run python services/core/tests/test_capabilities.py

The prompt carries a one-line capability index; the full contract of each tool
is fetched with ``tool.detail`` when she needs it. Both have to match the
registry exactly: a tool that exists but is not in the index is a capability she
does not know she has, and a detail page that omits an argument is worse than no
page at all.
"""

from __future__ import annotations

import asyncio
import sys

from capri_core.agent import prompt
from capri_core.tools import builtin_registry
from capri_core.tools.base import (
    ToolContext,
    ToolSpec,
    consent_note,
    describe_tool,
    signature,
)

FAILURES: list[str] = []


def check(condition: bool, label: str) -> None:
    if condition:
        print(f"  ok   {label}")
    else:
        print(f"  FAIL {label}")
        FAILURES.append(label)


def context() -> ToolContext:
    return ToolContext(conn=None, turn_id="t", subject="default", workspace=".")


async def main() -> int:
    registry = builtin_registry()
    specs = registry.specs()
    catalogue = prompt.describe_tools(specs)
    rows = [row for row in catalogue.splitlines() if row.startswith("- ")]
    system = prompt.system_prompt(specs, "kyoko", profile="work", request="close roblox studio")

    print("every tool is in the index")
    check(len(rows) == len(specs), f"one line per tool ({len(rows)} lines, {len(specs)} tools)")
    for spec in specs:
        check(f"- {signature(spec)}" in catalogue, f"{spec.name} is listed with its signature")
        check(spec.description.strip() != "", f"{spec.name} has a description")
        check(spec.name in system, f"{spec.name} reaches the prompt")

    print("the index states consent")
    for spec in specs:
        row = next((line for line in rows if line.startswith(f"- {spec.name}(")), "")
        note = consent_note(spec)
        if note == "asks the person first":
            check("[asks]" in row, f"{spec.name} says '[asks]'")
        elif note:
            check(f"[{note}]" in row, f"{spec.name} says {note!r}")
        else:
            check("[" not in row.split("— ", 1)[-1], f"{spec.name} states no consent tag")
    check("reads ask first" in catalogue, "clipboard says its reads ask first")
    check("can ask first for a destructive target" in catalogue, "a destructive click says it may ask")

    print("the index is an index")
    contracts = sum(len(describe_tool(spec)) for spec in specs)
    check(
        len(catalogue) < contracts,
        f"the index is smaller than the contracts ({len(catalogue)} vs {contracts} chars)",
    )
    check(any(row.endswith("…") for row in rows), "a long description is cut in the index")
    patch_spec = registry.get("fs.patch")
    patch_row = next(row for row in rows if row.startswith("- fs.patch("))
    check(
        "Every edit applies or none do" not in patch_row,
        "sentences after the first do not reach the prompt",
    )
    check(
        "Every edit applies or none do" in describe_tool(patch_spec),
        "but they are in the detail",
    )

    print("detail on request")
    check("tool.detail(name)" in system, "the prompt points at tool.detail")
    detail = await registry.call("tool.detail", context(), {"name": "fs.patch"})
    check(detail.ok, f"detail answers for a real tool: {detail.output.splitlines()[:1]}")
    check("Every edit applies or none do" in detail.output, "detail carries the full description")
    check("path (string, required)" in detail.output, "and every argument with its type")
    check("Tier 2 — asks the person first." in detail.output, "and how the call is approved")
    unknown = await registry.call("tool.detail", context(), {"name": "fs.pach"})
    check(not unknown.ok, "an unknown tool is an error, not an empty page")
    check("fs.patch" in unknown.output, f"with a suggestion: {unknown.output[:80]!r}")

    print("capabilities added later are known")
    early = prompt.describe_tools(registry.specs())
    late_tool = ToolSpec(
        name="notes.write",
        description="Write one line to her own notebook. Kept short on purpose.",
        params={
            "type": "object",
            "properties": {"line": {"type": "string"}},
            "required": ["line"],
            "additionalProperties": False,
        },
        tier=1,
        source="mcp:notes",
        version="mcp",
    )
    registry.register(late_tool, None)  # type: ignore[arg-type] - only the manifest matters
    late = prompt.describe_tools(registry.specs())
    check("notes.write" not in early, "the prompt before the tool existed has no row for it")
    check("notes.write(line)" in late, "and the next prompt does, with its signature")
    late_detail = await registry.call("tool.detail", context(), {"name": "notes.write"})
    check(late_detail.ok and "From mcp:notes" in late_detail.output, "detail knows where it came from")

    print("apps she may drive")
    apps = ("notepad.exe", "code.exe")
    reachable = prompt.system_prompt(specs, "kyoko", profile="work", apps=apps)
    check("## Reaching other apps" in reachable, "the allowlist is stated when screen tools exist")
    check("notepad.exe" in reachable and "code.exe" in reachable, "every allowed app is named")
    check("Policy refuses" in reachable, "and that anything else is refused")
    plain = prompt.system_prompt(specs, "kyoko", profile="work")
    check("## Reaching other apps" not in plain, "no such block without an allowlist")
    no_screen = [spec for spec in specs if not spec.name.startswith("screen.")]
    check(
        prompt.apps_block(apps, no_screen) == "",
        "and none when there are no screen tools to use it with",
    )

    print()
    if FAILURES:
        print(f"FAILED: {len(FAILURES)} check(s)")
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
