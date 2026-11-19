"""Blackwall: staged, reviewed, sandboxed dynamic tools and recipes."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import shutil
import sqlite3
import time
import uuid
from difflib import unified_diff
from pathlib import Path
from typing import Any, Awaitable, Callable

from .. import paths
from ..store import db
from ..tools.base import ToolContext, ToolResult, ToolSpec
from ..tools.policy import Policy
from ..tools.registry import Registry
from ..tools.schema import validate
from .audit import AuditReport, Finding, audit_source
from .broker import Broker
from .manifest import (
    WRITE_METHODS,
    ManifestError,
    ToolManifest,
    load_manifest,
    parse_manifest,
)
from .sandbox import run_call, run_tests
from .worker import Worker, WorkerError

log = logging.getLogger("capri.blackwall")

PROPOSE_SPEC = ToolSpec(
    name="tool.propose",
    mutates=True,
    description=(
        "Propose a new sandboxed tool package. Provide a manifest, a Python "
        "implementation with a run(args) function, and a test_impl.py with a "
        "test() function. The package is linted and run in an isolated process "
        "before the user is asked to approve that exact version."
    ),
    params={
        "type": "object",
        "properties": {
            "manifest": {"type": "object"},
            "implementation": {"type": "string"},
            "tests": {"type": "string"},
        },
        "required": ["manifest", "implementation", "tests"],
        "additionalProperties": False,
    },
    tier=0,
    # The approval card is part of this call, so the dispatch timeout must
    # outlive the human decision window (the card itself defaults to 180 s).
    timeout_ms=240_000,
    max_calls_per_turn=2,
)


class BlackwallError(ValueError):
    pass


def _version_key(value: str) -> tuple[int, ...]:
    return tuple(int(part) for part in value.split("."))


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class Blackwall:
    def __init__(
        self,
        registry: Registry,
        conn: sqlite3.Connection,
        policy: Policy,
        root: Path | None = None,
        critic: Callable[[dict[str, Any]], Awaitable[dict[str, Any]]] | None = None,
    ) -> None:
        self.registry = registry
        self.conn = conn
        self.policy = policy
        self.root = root or paths.blackwall_dir()
        self.proposals_dir = self.root / "proposals"
        self.installed_dir = self.root / "installed"
        self.quarantine_dir = self.root / "quarantine"
        for directory in (self.proposals_dir, self.installed_dir, self.quarantine_dir):
            directory.mkdir(parents=True, exist_ok=True)
        self._active: dict[str, tuple[str, ToolSpec, Path]] = {}
        self._recipe_names: set[str] = set()
        self._workers: dict[str, Worker] = {}
        self._broker = Broker()
        self._net: dict[str, dict[str, Any]] = {}
        # First write per installed version needs one explicit yes.
        self._write_ok: set[str] = set()
        self.critic = critic
        self.registry.on_quarantine = self._on_quarantine

    # ------------------------------------------------------------------ startup

    def load(self) -> int:
        """Load approved tools and recipes from the shard."""
        loaded = 0
        for name_dir in sorted(self.installed_dir.iterdir()):
            if not name_dir.is_dir():
                continue
            versions = [
                p
                for p in name_dir.iterdir()
                if p.is_dir()
                and (p / "manifest.json").is_file()
                and re.fullmatch(r"\d+\.\d+\.\d+", p.name)
            ]
            if not versions:
                continue
            current = name_dir / "current.txt"
            candidates: list[Path] = []
            current_name = ""
            try:
                current_name = current.read_text(encoding="utf-8").strip() if current.is_file() else ""
                if re.fullmatch(r"\d+\.\d+\.\d+", current_name):
                    candidate = name_dir / current_name
                    if candidate.is_dir():
                        candidates.append(candidate)
            except OSError:
                log.exception("could not read current version for %s", name_dir.name)
            for candidate in sorted(versions, key=lambda p: _version_key(p.name), reverse=True):
                if candidate not in candidates:
                    candidates.append(candidate)
            for selected in candidates:
                try:
                    self._register_package(selected)
                except Exception:
                    log.exception("could not load Blackwall package %s", selected)
                    continue
                loaded += 1
                if current_name != selected.name:
                    try:
                        current.write_text(selected.name, encoding="utf-8")
                    except OSError:
                        log.exception("could not repair current version for %s", name_dir.name)
                break
        loaded += self.load_recipes()
        if loaded:
            log.info("blackwall loaded %d tool(s)", loaded)
        return loaded

    def load_recipes(self) -> int:
        count = 0
        rows = self.conn.execute(
            "SELECT name, description, steps FROM skills"
            " WHERE name LIKE 'recipe.%' ORDER BY name"
        ).fetchall()
        for row in rows:
            try:
                steps = json.loads(row["steps"])
                spec = self._recipe_spec(row["name"], row["description"] or "", steps)
                if self.registry.get(spec.name) is None:
                    self._register_recipe(spec, steps)
                    count += 1
            except Exception:
                log.exception("could not load recipe %s", row["name"])
        return count

    # ------------------------------------------------------------------ propose

    async def propose_tool(
        self,
        ctx: ToolContext,
        *,
        manifest: dict[str, Any],
        implementation: str,
        tests: str,
    ) -> ToolResult:
        staged = await asyncio.to_thread(self.stage, manifest, implementation, tests)
        if not staged.get("ok"):
            return ToolResult.error(str(staged.get("output") or staged.get("reason")))
        proposal_id = str(staged["proposalId"])
        if ctx.confirm is None:
            self.reject(proposal_id)
            return ToolResult.error("this tool needs a user approval channel")
        review = await self._review(staged, implementation, tests)
        if review.get("verdict") == "reject":
            self.reject(proposal_id)
            return ToolResult.error(
                f"critic rejected the package: {review.get('reason') or 'unsafe capability use'}"
            )
        details = {
            "name": staged["name"],
            "version": staged["version"],
            "capabilities": staged["capabilities"],
            "network": staged.get("network"),
            "audit": staged["audit"],
            "review": review,
            "tests": staged["testOutput"],
            "testSource": tests[:6000],
            "diff": self._proposal_diff(str(staged["name"]), str(staged["version"]), implementation),
        }
        approved = await ctx.confirm(
            {
                "turnId": ctx.turn_id,
                "confirmId": f"bw_{uuid.uuid4().hex[:10]}",
                "tool": "tool.propose",
                "args": details,
                "tier": 4,
                "tierName": "blackwall",
                "reason": f"install {staged['name']} {staged['version']} in the sandbox",
                "timeout": 180,
            }
        )
        if not approved:
            self.reject(proposal_id)
            return ToolResult(ok=False, output="tool installation denied by the user")
        installed = self.install(proposal_id)
        if not installed.get("ok"):
            return ToolResult.error(str(installed.get("output")))
        return ToolResult(
            ok=True,
            output=(
                f"installed {installed['name']} {installed['version']}; "
                "it is available on the next turn"
            ),
            data=installed,
        )

    def stage(self, manifest: dict[str, Any], implementation: str, tests: str) -> dict[str, Any]:
        try:
            parsed = parse_manifest(manifest)
        except ManifestError as exc:
            return {"ok": False, "output": f"manifest rejected: {exc}"}
        if parsed.name == "tool.propose" or parsed.name.startswith("recipe."):
            return {"ok": False, "output": f"{parsed.name!r} is a reserved Blackwall name"}
        if self.registry.get(parsed.name) is not None and parsed.name not in self._active:
            return {"ok": False, "output": f"{parsed.name!r} is a built-in tool name"}
        if not implementation.strip():
            return {"ok": False, "output": "implementation is empty"}
        if not tests.strip():
            return {"ok": False, "output": "tests are required"}
        caps = parsed.capabilities
        if caps.network and not caps.network_rules:
            return {
                "ok": False,
                "output": (
                    "network capability needs declared hosts, e.g. capabilities.network = "
                    '{"hosts": [{"host": "api.github.com", "methods": ["GET"]}]}; '
                    "open egress is not a permission"
                ),
            }
        if not caps.network and caps.network_rules:
            return {
                "ok": False,
                "output": "network rules need capabilities.network = true",
            }
        if caps.subprocess:
            return {
                "ok": False,
                "output": "Blackwall v1 does not permit subprocess capability; use a normal built-in tool",
            }
        if caps.screen:
            return {
                "ok": False,
                "output": "Blackwall v1 does not permit screen capability",
            }

        report = audit_source(implementation, caps)
        test_report = audit_source(tests, caps)
        findings = report.findings + [
            Finding(
                severity=finding.severity,
                code="test." + finding.code,
                message=finding.message,
                line=finding.line,
            )
            for finding in test_report.findings
        ]
        combined = AuditReport(findings)
        if not combined.ok:
            lines = [f"{f.severity}: line {f.line}: {f.message}" for f in combined.findings]
            return {
                "ok": False,
                "output": "static audit rejected the package:\n" + "\n".join(lines),
                "audit": combined.payload(),
            }

        proposal_id = f"bwp_{uuid.uuid4().hex[:12]}"
        package = self.proposals_dir / proposal_id
        package.mkdir(parents=True, exist_ok=True)
        (package / "manifest.json").write_text(
            json.dumps(parsed.model_dump(mode="json"), indent=2), encoding="utf-8"
        )
        (package / "impl.py").write_text(implementation, encoding="utf-8")
        (package / "test_impl.py").write_text(tests, encoding="utf-8")
        digests = {
            "sha256": _sha(implementation),
            "testsSha": _sha(tests),
            "manifestSha": _manifest_digest(parsed),
        }

        test_result = run_tests(package, parsed)
        if test_result.get("ok") and not _package_matches(package, digests):
            test_result = {
                "ok": False,
                "output": "sandbox tests modified the package; refusing the proposal",
            }
        test_output = str(test_result.get("output") or "")[-4000:]
        record = {
            "ok": bool(test_result.get("ok")),
            "proposalId": proposal_id,
            "name": parsed.name,
            "version": parsed.version,
            "capabilities": caps.model_dump(mode="json"),
            "network": caps.network_card(),
            "audit": combined.payload(),
            "testOutput": test_output,
            **digests,
            "createdAt": int(time.time() * 1000),
            "status": "tested" if test_result.get("ok") else "test_failed",
        }
        (package / "proposal.json").write_text(
            json.dumps(record, indent=2), encoding="utf-8"
        )
        if not test_result.get("ok"):
            record["output"] = f"sandbox tests failed:\n{test_output}"
        return record

    def install(self, proposal_id: str) -> dict[str, Any]:
        package = self.proposals_dir / proposal_id
        meta_path = package / "proposal.json"
        if not meta_path.is_file():
            return {"ok": False, "output": f"no proposal {proposal_id!r}"}
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            manifest = load_manifest(package)
        except (OSError, ValueError) as exc:
            return {"ok": False, "output": f"proposal is unreadable: {exc}"}
        if not meta.get("ok"):
            return {"ok": False, "output": "proposal did not pass its sandbox tests"}
        digests = {
            "sha256": meta.get("sha256"),
            "testsSha": meta.get("testsSha"),
            "manifestSha": meta.get("manifestSha"),
        }
        if not _package_matches(package, digests) or digests["manifestSha"] != _manifest_digest(manifest):
            return {"ok": False, "output": "proposal files changed after approval"}

        target = self.installed_dir / manifest.name / manifest.version
        if target.exists():
            return {"ok": False, "output": f"{manifest.name} {manifest.version} is already installed"}
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(package, target, dirs_exist_ok=True)

        previous = self._active.get(manifest.name)
        if previous is not None:
            self._close_worker(manifest.name)
            self.registry.unregister(manifest.name)
        try:
            self._register_package(target)
        except Exception as exc:
            if previous is not None:
                self._register_package(previous[2])
            return {"ok": False, "output": f"could not register {manifest.name}: {exc}"}
        (target.parent / "current.txt").write_text(manifest.version, encoding="utf-8")
        meta["status"] = "installed"
        meta["installedAt"] = int(time.time() * 1000)
        meta_path.write_text(json.dumps(meta, indent=2), encoding="utf-8")
        try:
            db.set_tool_quarantined(self.conn, manifest.name, False)
        except Exception:
            log.exception("could not clear quarantine flag for %s", manifest.name)
        return {
            "ok": True,
            "name": manifest.name,
            "version": manifest.version,
            "path": str(target),
        }

    async def _review(
        self,
        staged: dict[str, Any],
        implementation: str,
        tests: str,
    ) -> dict[str, Any]:
        if self.critic is None:
            return {
                "verdict": "skipped",
                "reason": "no model critic configured",
                "concerns": [],
                "capability_diff": [],
            }
        try:
            review = await self.critic(
                {
                    "name": staged["name"],
                    "version": staged["version"],
                    "capabilities": staged["capabilities"],
                    "audit": staged["audit"],
                    "implementation": implementation[:12_000],
                    "tests": tests[:8_000],
                }
            )
        except Exception as exc:
            log.exception("Blackwall critic failed")
            return {
                "verdict": "unavailable",
                "reason": f"critic failed: {exc}",
                "concerns": [],
                "capability_diff": [],
            }
        if not isinstance(review, dict):
            return {
                "verdict": "unavailable",
                "reason": "critic returned a non-object",
                "concerns": [],
                "capability_diff": [],
            }
        verdict = str(review.get("verdict") or "unavailable").lower()
        if verdict not in {"approve", "reject", "unavailable"}:
            verdict = "unavailable"
        concerns = review.get("concerns")
        capability_diff = review.get("capability_diff")
        return {
            "verdict": verdict,
            "reason": str(review.get("reason") or ""),
            "concerns": [str(item) for item in concerns] if isinstance(concerns, list) else [],
            "capability_diff": (
                [str(item) for item in capability_diff]
                if isinstance(capability_diff, list)
                else []
            ),
        }

    def _proposal_diff(self, name: str, version: str, implementation: str) -> str:
        previous = self._active.get(name)
        old = ""
        old_label = "/dev/null"
        if previous is not None:
            old_label = f"{name}/{previous[0]}/impl.py"
            try:
                old = (previous[2] / "impl.py").read_text(encoding="utf-8")
            except OSError:
                old = ""
        diff = unified_diff(
            old.splitlines(keepends=True),
            implementation.splitlines(keepends=True),
            fromfile=old_label,
            tofile=f"{name}/{version}/impl.py",
            lineterm="",
        )
        return "".join(diff)[:12_000]

    def reject(self, proposal_id: str) -> None:
        package = self.proposals_dir / proposal_id
        meta_path = package / "proposal.json"
        if meta_path.is_file():
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
                meta["status"] = "rejected"
                meta_path.write_text(json.dumps(meta, indent=2), encoding="utf-8")
            except (OSError, ValueError):
                pass

    # ------------------------------------------------------------------ runtime

    def _register_package(self, package: Path) -> None:
        manifest = load_manifest(package)
        if manifest.name == "tool.propose" or manifest.name.startswith("recipe."):
            raise BlackwallError(f"{manifest.name!r} is a reserved Blackwall name")
        spec = manifest.spec()

        async def handler(ctx: ToolContext, **args: Any) -> ToolResult:
            return await self.call_tool(manifest.name, package, spec, ctx, args)

        self.registry.register(spec, handler)
        self._active[manifest.name] = (manifest.version, spec, package)

    async def call_tool(
        self,
        name: str,
        package: Path,
        spec: ToolSpec,
        ctx: ToolContext,
        args: dict[str, Any],
    ) -> ToolResult:
        problems = validate(spec.params, args)
        if problems:
            return ToolResult.error(f"{name}: " + "; ".join(problems))
        manifest = load_manifest(package)
        refused = await self._confirm_first_write(manifest, ctx)
        if refused is not None:
            return refused
        net = self._net_for(name, manifest)
        worker = self._workers.get(name)
        if worker is not None and worker.package != package.resolve():
            worker.close()
            worker = None
        if worker is None:
            worker = Worker(package, manifest, net=net)
            self._workers[name] = worker
        try:
            result = await asyncio.to_thread(
                worker.call, args, max(0.1, manifest.timeout_ms / 1000)
            )
        except (WorkerError, OSError, ValueError):
            log.exception("Blackwall worker for %s failed; using a one-shot process", name)
            self._close_worker(name)
            net = self._net_for(name, manifest)
            result = await asyncio.to_thread(run_call, package, manifest, args, net)
        output = str(result.get("output") or "")
        return ToolResult(
            ok=bool(result.get("ok")),
            output=output[-20_000:] or ("ok" if result.get("ok") else "tool failed"),
            data=result.get("data") if isinstance(result.get("data"), dict) else None,
        )

    async def _confirm_first_write(
        self, manifest: ToolManifest, ctx: ToolContext
    ) -> ToolResult | None:
        """A tool that can write must be let out once, by the user."""
        writes = [rule for rule in manifest.capabilities.network_rules if rule.writes]
        if not writes:
            return None
        key = f"{manifest.name}@{manifest.version}"
        if key in self._write_ok:
            return None
        if ctx.confirm is None:
            return ToolResult.error(
                f"{manifest.name} can write over the network and needs a user "
                "approval channel before its first write"
            )
        methods: list[str] = []
        for rule in writes:
            for method in rule.methods:
                if method in WRITE_METHODS and method not in methods:
                    methods.append(method)
        approved = await ctx.confirm(
            {
                "turnId": ctx.turn_id,
                "confirmId": f"bwn_{uuid.uuid4().hex[:10]}",
                "tool": manifest.name,
                "args": {
                    "name": manifest.name,
                    "version": manifest.version,
                    "methods": methods,
                    "hosts": [rule.host for rule in writes],
                },
                "tier": 4,
                "tierName": "blackwall",
                "reason": (
                    f"first write for {manifest.name}: it may "
                    f"{'/'.join(methods)} to {', '.join(rule.host for rule in writes)}"
                ),
                "timeout": 120,
            }
        )
        if not approved:
            return ToolResult(ok=False, output="first network write denied by the user")
        self._write_ok.add(key)
        return None

    def _net_for(self, name: str, manifest: ToolManifest) -> dict[str, Any] | None:
        """One broker grant per tool, reused by its warm worker."""
        existing = self._net.get(name)
        if existing is not None:
            return existing
        if not manifest.capabilities.network_rules:
            return None
        net = self._broker.grant(
            name,
            manifest.capabilities.network_rules,
            manifest.capabilities.network_secrets,
        )
        self._net[name] = net
        return net

    def _close_worker(self, name: str) -> None:
        worker = self._workers.pop(name, None)
        if worker is not None:
            worker.close()
        net = self._net.pop(name, None)
        if net is not None:
            self._broker.revoke(str(net.get("token") or ""))

    def close(self) -> None:
        for name in list(self._workers):
            self._close_worker(name)
        for token in list(self._net.values()):
            self._broker.revoke(str(token.get("token") or ""))
        self._net.clear()
        self._broker.close()

    def _on_quarantine(self, name: str) -> None:
        if name in self._active:
            self.quarantine(name)

    def quarantine(self, name: str) -> dict[str, Any]:
        active = self._active.pop(name, None)
        if active is None:
            return {"ok": False, "output": f"{name!r} is not a Blackwall tool"}
        version, _spec, package = active
        self._close_worker(name)
        self.registry.unregister(name)
        destination = self.quarantine_dir / f"{name}-{version}-{int(time.time())}"
        try:
            shutil.move(str(package), str(destination))
        except OSError:
            log.exception("could not move quarantined package %s", package)
        try:
            db.set_tool_quarantined(self.conn, name, True)
        except Exception:
            log.exception("could not mark %s quarantined", name)
        # One automatic rollback to the newest remaining installed version.
        restored = None
        versions = [
            p
            for p in (self.installed_dir / name).iterdir()
            if p.is_dir()
            and (p / "manifest.json").is_file()
            and p.resolve() != package.resolve()
        ] if (self.installed_dir / name).is_dir() else []
        versions.sort(key=lambda p: _version_key(p.name), reverse=True)
        for candidate in versions:
            try:
                self._register_package(candidate)
                (self.installed_dir / name / "current.txt").write_text(
                    candidate.name, encoding="utf-8"
                )
                restored = candidate.name
                break
            except Exception:
                log.exception("could not restore %s %s", name, candidate.name)
        return {
            "ok": True,
            "name": name,
            "quarantined": version,
            "restored": restored,
        }

    # ------------------------------------------------------------------ recipes

    def install_recipe(self, raw: dict[str, Any]) -> dict[str, Any]:
        try:
            name = str(raw.get("name") or "").strip()
            if not name.startswith("recipe."):
                name = "recipe." + name
            if not re.fullmatch(r"recipe\.[a-z][a-z0-9_]*", name):
                raise BlackwallError("recipe name must be recipe.<lowercase_words>")
            description = str(raw.get("description") or "").strip()
            params = raw.get("params") or {"type": "object", "properties": {}}
            steps = raw.get("steps")
            if not isinstance(steps, list) or not steps:
                raise BlackwallError("steps must be a non-empty list")
            spec = self._recipe_spec(name, description, steps, params=params)
            self._validate_recipe_steps(steps)
            self.conn.execute(
                "INSERT INTO skills (name, description, steps, created_at) VALUES (?, ?, ?, ?)"
                " ON CONFLICT(name) DO UPDATE SET description = excluded.description,"
                " steps = excluded.steps",
                (name, description, json.dumps(steps), int(time.time() * 1000)),
            )
            if self.registry.get(name) is not None:
                self.registry.unregister(name)
            self._register_recipe(spec, steps)
            return {"ok": True, "name": name}
        except (BlackwallError, ValueError) as exc:
            return {"ok": False, "output": str(exc)}

    def _recipe_spec(
        self,
        name: str,
        description: str,
        steps: list[dict[str, Any]],
        *,
        params: dict[str, Any] | None = None,
    ) -> ToolSpec:
        if self.registry.get(name) is not None and name not in self._recipe_names:
            raise BlackwallError(f"{name!r} is already a registered tool")
        params = params or {"type": "object", "properties": {}}
        if not isinstance(params, dict) or params.get("type", "object") != "object":
            raise BlackwallError("params must be a JSON Schema object")
        tier = 0
        for step in steps:
            spec = self.registry.get(str(step.get("tool") or ""))
            if spec is None:
                raise BlackwallError(f"unknown step tool {step.get('tool')!r}")
            tier = max(tier, spec.tier)
        if tier >= 2:
            raise BlackwallError("recipes may only chain observe/benign tools")
        return ToolSpec(
            name=name,
            description=description or "A saved Blackwall recipe.",
            params=params,
            tier=tier,
            timeout_ms=30_000,
            max_calls_per_turn=2,
            version="recipe",
            source="recipe",
        )

    def _validate_recipe_steps(self, steps: list[dict[str, Any]]) -> None:
        for step in steps:
            if not isinstance(step, dict):
                raise BlackwallError("each step must be an object")
            name = str(step.get("tool") or "")
            if name in {"tool.propose"} or name.startswith("recipe."):
                raise BlackwallError("recipes may not chain tool.propose or another recipe")
            spec = self.registry.get(name)
            if spec is None:
                raise BlackwallError(f"unknown step tool {name!r}")
            if spec.tier >= 2:
                raise BlackwallError(f"step {name!r} is destructive; recipes are observe/benign only")
            if not isinstance(step.get("args", {}), dict):
                raise BlackwallError(f"step {name!r} args must be an object")

    def _register_recipe(self, spec: ToolSpec, steps: list[dict[str, Any]]) -> None:
        async def handler(ctx: ToolContext, **args: Any) -> ToolResult:
            return await self._run_recipe(spec, steps, ctx, args)

        self.registry.register(spec, handler)
        self._recipe_names.add(spec.name)

    async def _run_recipe(
        self,
        spec: ToolSpec,
        steps: list[dict[str, Any]],
        ctx: ToolContext,
        args: dict[str, Any],
    ) -> ToolResult:
        problems = validate(spec.params, args)
        if problems:
            return ToolResult.error(f"{spec.name}: " + "; ".join(problems))
        outputs: list[str] = []
        for step in steps:
            name = str(step.get("tool") or "")
            step_spec = self.registry.get(name)
            if step_spec is None:
                return ToolResult.error(f"recipe step {name!r} is no longer available")
            call_args = _substitute(step.get("args", {}), args)
            if not isinstance(call_args, dict):
                return ToolResult.error(f"recipe step {name!r} produced invalid args")
            verdict = self.policy.check(step_spec, call_args)
            if verdict.verdict != "allow":
                return ToolResult.error(f"recipe step {name!r} blocked by policy: {verdict.reason}")
            result = await self.registry.call(name, ctx, call_args)
            if not result.ok:
                return ToolResult.error(
                    f"recipe {spec.name} failed at {name}: {result.preview(800)}"
                )
            outputs.append(f"[{name}]\n{result.preview(1200)}")
        return ToolResult(ok=True, output="\n\n".join(outputs))

    def list_proposals(self) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        for path in sorted(self.proposals_dir.glob("*/proposal.json")):
            try:
                items.append(json.loads(path.read_text(encoding="utf-8")))
            except (OSError, ValueError):
                continue
        return items

    def list_installed(self) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        for name, (version, _spec, package) in sorted(self._active.items()):
            items.append({"name": name, "version": version, "path": str(package)})
        return items

    def list_recipes(self) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT name, description, steps, success_count, fail_count FROM skills ORDER BY name"
        ).fetchall()
        return [dict(row) for row in rows]


def _manifest_digest(manifest: ToolManifest) -> str:
    return _sha(json.dumps(manifest.model_dump(mode="json"), sort_keys=True))


def _package_matches(package: Path, digests: dict[str, str | None]) -> bool:
    """The sandbox must not be able to change the code the user approves."""
    try:
        current = {
            "sha256": _sha((package / "impl.py").read_text(encoding="utf-8")),
            "testsSha": _sha((package / "test_impl.py").read_text(encoding="utf-8")),
        }
        if digests.get("manifestSha") is not None:
            current["manifestSha"] = _manifest_digest(load_manifest(package))
    except (OSError, ValueError):
        return False
    return all(digests.get(key) == value for key, value in current.items())


def _substitute(value: Any, args: dict[str, Any]) -> Any:
    if isinstance(value, str):
        match = re.fullmatch(r"\{([A-Za-z_][A-Za-z0-9_]*)\}", value)
        if match:
            return args.get(match.group(1), value)
        return re.sub(
            r"\{([A-Za-z_][A-Za-z0-9_]*)\}",
            lambda m: str(args.get(m.group(1), m.group(0))),
            value,
        )
    if isinstance(value, list):
        return [_substitute(item, args) for item in value]
    if isinstance(value, dict):
        return {key: _substitute(item, args) for key, item in value.items()}
    return value
