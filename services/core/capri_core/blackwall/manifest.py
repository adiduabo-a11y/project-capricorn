"""Blackwall tool manifests.

A dynamic tool is a small package: a manifest, a Python entry point, and one
test module. The manifest is the contract the planner and policy see; the entry
point never runs in the daemon's process.
"""

from __future__ import annotations

import ipaddress
import re
from pathlib import Path
from typing import Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

from ..tools.base import ToolSpec

NAME_RE = re.compile(r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)?$")
VERSION_RE = re.compile(r"^\d+\.\d+\.\d+$")
ENTRY_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*\.py):([A-Za-z_][A-Za-z0-9_]*)$")


class ManifestError(ValueError):
    pass


METHODS = ("GET", "HEAD", "POST", "PUT", "PATCH", "DELETE")
WRITE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
# A rule above either line is marked "high budget" on the approval card.
HIGH_REQUESTS_PER_HOUR = 300
HIGH_RESPONSE_BYTES = 2 * 1_024 * 1_024
HOST_RE = re.compile(
    r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)+"
)
SECRET_RE = re.compile(r"[A-Z][A-Z0-9_]{1,63}")
HEADER_RE = re.compile(r"[A-Za-z0-9-]{1,64}")


def _clean_host(raw: str, *, wildcard: bool) -> str:
    host = str(raw or "").strip().lower().rstrip(".")
    if not host:
        raise ValueError("host entries must name a host, e.g. api.github.com")
    if "://" in host or "/" in host or " " in host or "@" in host:
        raise ValueError("hosts are bare hostnames, with no scheme, path or credentials")
    if host.startswith("*."):
        if not wildcard:
            raise ValueError("wildcards are only allowed in a host rule")
        label = host[2:]
        if not label or "*" in label or "." not in label or not HOST_RE.fullmatch(label):
            raise ValueError("a wildcard must look like *.example.com")
        return host
    if "*" in host:
        raise ValueError("only a leading * wildcard is supported")
    try:
        ipaddress.ip_address(host)
        return host
    except ValueError:
        pass
    if not HOST_RE.fullmatch(host):
        raise ValueError(f"{host!r} is not a hostname")
    return host


class HostRule(BaseModel):
    """One declared destination: host, scheme, methods, paths and a budget."""

    model_config = ConfigDict(extra="forbid")

    host: str
    scheme: Literal["https", "http"] = "https"
    methods: list[str] = Field(default_factory=lambda: ["GET"])
    paths: list[str] = Field(default_factory=lambda: ["/*"])
    max_response_bytes: int = Field(default=262_144, ge=1_024, le=8 * 1_024 * 1_024)
    requests_per_hour: int = Field(default=30, ge=1, le=600)
    timeout_s: float = Field(default=15.0, gt=0, le=60)

    @field_validator("host")
    @classmethod
    def _host(cls, value: str) -> str:
        return _clean_host(value, wildcard=True)

    @field_validator("methods")
    @classmethod
    def _methods(cls, value: list[str]) -> list[str]:
        cleaned: list[str] = []
        for raw in value:
            method = str(raw).strip().upper()
            if method not in METHODS:
                raise ValueError(f"methods must be one of {', '.join(METHODS)}")
            if method not in cleaned:
                cleaned.append(method)
        if not cleaned:
            raise ValueError("a host rule needs at least one method")
        return cleaned

    @field_validator("paths")
    @classmethod
    def _paths(cls, value: list[str]) -> list[str]:
        cleaned: list[str] = []
        for raw in value:
            path = str(raw).strip()
            if not path.startswith("/") or " " in path:
                raise ValueError("paths are URL paths starting with /, e.g. /repos/*")
            if path not in cleaned:
                cleaned.append(path)
        if not cleaned:
            raise ValueError("a host rule needs at least one path")
        return cleaned

    @property
    def writes(self) -> bool:
        return any(method in WRITE_METHODS for method in self.methods)

    @property
    def high_budget(self) -> bool:
        return (
            self.requests_per_hour > HIGH_REQUESTS_PER_HOUR
            or self.max_response_bytes > HIGH_RESPONSE_BYTES
        )

    def card(self) -> dict[str, Any]:
        return {
            "host": self.host,
            "scheme": self.scheme,
            "methods": list(self.methods),
            "paths": list(self.paths),
            "maxResponseBytes": self.max_response_bytes,
            "requestsPerHour": self.requests_per_hour,
            "timeoutSec": self.timeout_s,
            "writes": self.writes,
            "highBudget": self.high_budget,
        }


class SecretRule(BaseModel):
    """A named secret the broker adds to requests for the listed hosts."""

    model_config = ConfigDict(extra="forbid")

    name: str
    header: str = "Authorization"
    prefix: str = ""
    hosts: list[str] = Field(default_factory=list)

    @field_validator("name")
    @classmethod
    def _name(cls, value: str) -> str:
        name = str(value).strip()
        if not SECRET_RE.fullmatch(name):
            raise ValueError("secret names are UPPER_SNAKE_CASE, e.g. GITHUB_TOKEN")
        return name

    @field_validator("header")
    @classmethod
    def _header(cls, value: str) -> str:
        header = str(value).strip()
        if not HEADER_RE.fullmatch(header):
            raise ValueError("header must be a plain HTTP header name")
        return "-".join(part.capitalize() for part in header.split("-"))

    @field_validator("prefix")
    @classmethod
    def _prefix(cls, value: str) -> str:
        prefix = str(value)
        if "\n" in prefix or "\r" in prefix:
            raise ValueError("prefix must stay on one line")
        return prefix

    @field_validator("hosts")
    @classmethod
    def _hosts(cls, value: list[str]) -> list[str]:
        cleaned: list[str] = []
        for raw in value:
            host = _clean_host(raw, wildcard=True)
            if host not in cleaned:
                cleaned.append(host)
        if not cleaned:
            raise ValueError("name the hosts this secret is sent to")
        return cleaned

    def card(self) -> dict[str, Any]:
        return {"name": self.name, "header": self.header, "hosts": list(self.hosts)}


class Capabilities(BaseModel):
    model_config = ConfigDict(extra="forbid")

    network: bool = False
    # v2: declared destinations. A tool never opens a socket of its own; the
    # broker fetches on its behalf and enforces the rule that matched.
    network_rules: list[HostRule] = Field(default_factory=list)
    network_secrets: list[SecretRule] = Field(default_factory=list)
    subprocess: bool = False
    read_files: bool = False
    write_files: bool = False
    screen: bool = False

    @model_validator(mode="before")
    @classmethod
    def _network_shapes(cls, data: Any) -> Any:
        """Accept ``network: {...}`` and the v1.1 ``network_hosts`` shape."""
        if not isinstance(data, dict):
            return data
        raw = dict(data)
        network = raw.get("network")
        if isinstance(network, dict):
            raw["network"] = True
            raw["network_rules"] = network.get("hosts") or raw.get("network_rules") or []
            raw["network_secrets"] = network.get("secrets") or raw.get("network_secrets") or []
            raw.pop("network_hosts", None)
        legacy = raw.get("network_hosts") or []
        if legacy and not raw.get("network_rules"):
            # v1.1 manifests only named hosts; that is a read-only https grant.
            raw["network_rules"] = [{"host": host} for host in legacy]
        raw.pop("network_hosts", None)
        return raw

    def network_card(self) -> dict[str, Any] | None:
        if not self.network and not self.network_rules:
            return None
        return {
            "containment": "advisory",
            "note": (
                "the tool process has no sockets; requests go through the daemon "
                "broker, but this is an in-process boundary, not a network namespace"
            ),
            "hosts": [rule.card() for rule in self.network_rules],
            "secrets": [secret.card() for secret in self.network_secrets],
        }


class ToolManifest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    version: str = "0.1.0"
    description: str = Field(min_length=1, max_length=500)
    params: dict[str, Any] = Field(default_factory=dict)
    tier: int = Field(default=0, ge=0, le=2)
    timeout_ms: int = Field(default=8_000, ge=100, le=30_000)
    max_calls_per_turn: int = Field(default=4, ge=1, le=8)
    entry: str = "impl.py:run"
    capabilities: Capabilities = Field(default_factory=Capabilities)

    @field_validator("name")
    @classmethod
    def _name(cls, value: str) -> str:
        if not NAME_RE.fullmatch(value):
            raise ValueError("name must be lowercase words, optionally one dot (e.g. text.count)")
        return value

    @field_validator("version")
    @classmethod
    def _version(cls, value: str) -> str:
        if not VERSION_RE.fullmatch(value):
            raise ValueError("version must be MAJOR.MINOR.PATCH")
        return value

    @field_validator("params")
    @classmethod
    def _params(cls, value: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(value, dict) or value.get("type", "object") != "object":
            raise ValueError("params must be a JSON Schema object")
        properties = value.get("properties", {})
        if not isinstance(properties, dict):
            raise ValueError("params.properties must be an object")
        return value

    @field_validator("entry")
    @classmethod
    def _entry(cls, value: str) -> str:
        match = ENTRY_RE.fullmatch(value)
        if not match:
            raise ValueError("entry must be a local Python file and function, e.g. impl.py:run")
        if Path(match.group(1)).name != match.group(1):
            raise ValueError("entry must name a file in the package root")
        return value

    @property
    def module_file(self) -> str:
        return self.entry.split(":", 1)[0]

    @property
    def function_name(self) -> str:
        return self.entry.split(":", 1)[1]

    def spec(self) -> ToolSpec:
        return ToolSpec(
            name=self.name,
            description=self.description,
            params=self.params,
            tier=self.tier,
            timeout_ms=self.timeout_ms,
            max_calls_per_turn=self.max_calls_per_turn,
            version=self.version,
            source="blackwall",
        )


def parse_manifest(raw: dict[str, Any]) -> ToolManifest:
    try:
        return ToolManifest.model_validate(raw)
    except ValidationError as exc:
        raise ManifestError(str(exc)) from exc


def load_manifest(package: Path) -> ToolManifest:
    path = package / "manifest.json"
    try:
        import json

        return parse_manifest(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError) as exc:
        if isinstance(exc, ManifestError):
            raise
        raise ManifestError(f"could not read {path}: {exc}") from exc
