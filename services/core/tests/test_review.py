"""Destructive-call review: the veto list, the prompt and the answer.

    uv run python services/core/tests/test_review.py

No model: the reviewer is a string in, string out function, so the rules around
it — what can never be waived, what counts as a yes — are checked directly.
"""

from __future__ import annotations

import sys

from capri_core.tools.review import REVIEW_SPEC, judge, messages, veto

FAILURES: list[str] = []


def check(condition: bool, label: str) -> None:
    if condition:
        print(f"  ok   {label}")
    else:
        print(f"  FAIL {label}")
        FAILURES.append(label)


def main() -> int:
    print("veto")
    for command, why in (
        ("rm -rf /", "the root"),
        ("rm -rf ~/Documents", "the home directory"),
        ("sudo mkfs.ext4 /dev/sda1", "a filesystem"),
        ("dd if=/dev/zero of=/dev/sda", "raw blocks"),
        ("format D:", "a disk"),
        ("del /f /s /q C:\\Users\\me\\*", "files"),
        ("rd /s /q build", "a directory tree"),
        ("git push --force origin main", "a remote history"),
        ("git reset --hard HEAD~1", "uncommitted work"),
        ("git clean -fdx", "untracked work"),
        ("DROP TABLE memories", "stored data"),
        ("shutdown /s /t 0", "the machine"),
        ("Remove-Item -Recurse -Force C:\\work", "a directory tree"),
    ):
        check(veto("shell.run", {"command": command}) is not None, f"never auto-approved: {command}")

    for command in (
        "rm -rf build",
        "rm -rf ./node_modules",
        "npm run build",
        "git reset --soft HEAD~1",
        "git push origin main",
        "Remove-Item .\\tmp\\out.txt",
    ):
        check(veto("shell.run", {"command": command}) is None, f"may be reviewed: {command}")
    check(veto("fs.write", {"path": "notes.md"}) is None, "a tool inside the registry has no static veto")
    # Closing an app politely can be reviewed; killing it cannot be waived.
    check(
        veto("process.close", {"name": "roblox studio"}) is None,
        "a polite app close is reviewable",
    )
    check(
        veto("process.close", {"name": "roblox studio", "force": True}) is not None,
        "a forced kill is never auto-approved",
    )

    print("judge")
    yes = judge({"approve": True, "reversible": True, "blast": "build/", "reason": "removes a build artefact"})
    check(yes.approved, f"an explicit yes on both counts approves: {yes.reason}")
    check(
        not judge({"approve": True, "reversible": False, "reason": "deletes history"}).approved,
        "no undo, no approval",
    )
    check(
        not judge({"approve": False, "reversible": True, "reason": "unsure"}).approved,
        "an explicit no is a no",
    )
    check(not judge({"approve": "true", "reversible": "yes", "reason": "x"}).approved, "strings are not booleans")
    check(not judge({}).approved, "an empty answer is a no")
    check(
        not judge({"approve": True, "reversible": True, "reason": "   "}).approved,
        "a yes with no reason is a no",
    )

    print("prompt")
    sent = messages("shell.run", {"command": "rm -rf build", "cwd": "C:\\work"}, "arbitrary shell command", "C:\\work")
    text = "\n".join(str(message.get("content")) for message in sent)
    check("rm -rf build" in text, "the reviewer sees the command")
    check("C:\\work" in text, "and where it runs")
    check("arbitrary shell command" in text, "and why it stopped")
    check("data, never" in text, "the arguments are labelled as data, not instructions")
    check("Any doubt means do not approve" in text, "the prompt defaults to refusing")
    # Her arguments are her output: on the assistant role, never mixed into the
    # instruction that the reviewer is meant to follow.
    roles = [message.get("role") for message in sent]
    check(roles == ["system", "assistant", "user"], f"the call is her message: {roles}")
    check("rm -rf build" in str(sent[1].get("content")), "the arguments ride on the assistant role")
    check(
        "rm -rf build" not in str(sent[-1].get("content")),
        "and never appear inside what the reviewer is told to do",
    )
    long = messages("shell.run", {"command": "x" * 5000}, "arbitrary shell command", "C:\\work")
    check(len(str(long[1].get("content"))) <= 2000, "a huge argument list is truncated")

    # The reviewer has to see what was asked, or "close the app" is indistinct
    # from "close an app nobody mentioned" and every close is refused.
    asked = messages(
        "process.close",
        {"name": "roblox studio"},
        "tool declares destructive effects",
        "C:\\work",
        "can you close roblox studio please",
    )
    asked_text = str(asked[-1].get("content"))
    check(
        'The person asked: "can you close roblox studio please"' in asked_text,
        "the reviewer sees the request",
    )
    silent = messages("process.close", {"name": "x"}, "reason", "C:\\work")
    check("The person asked" not in str(silent[-1].get("content")), "and nothing when there is none")
    huge = messages(
        "process.close", {"name": "x"}, "reason", "C:\\work", "y" * 900
    )
    check(len(str(huge[-1].get("content"))) < 700, "a long request is truncated")

    print("scope")
    from capri_core.tools.review import SYSTEM

    check("does what the person asked" in SYSTEM, "the reviewer judges against the request")
    check("started again" in SYSTEM, "a named app closing is reversible in its terms")
    check("with force" in SYSTEM, "a forced kill is not")
    check("data, never" in SYSTEM, "arguments are still data, not instructions")

    print("schema")
    required = REVIEW_SPEC.params.get("required", [])
    check(
        {"approve", "reversible", "reason"}.issubset(set(required)),
        f"the answer schema requires the fields the judge reads: {required}",
    )
    check(
        REVIEW_SPEC.params["properties"]["approve"]["type"] == "boolean",
        "approve is a boolean in the schema, not prose",
    )

    print()
    if FAILURES:
        print(f"FAILED: {len(FAILURES)} check(s)")
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
