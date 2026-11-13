"""Layered, schema-validated configuration.

Precedence:  model defaults  <  ~/.capri/config.yaml  <  <project>/.capri.yaml
             <  environment (CAPRI_SECTION__KEY)

The settings UI is generated from :func:`schema`, so any key added here appears
in the app automatically.
"""

from __future__ import annotations

import copy
import os
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from . import paths

_ENV_PREFIX = "CAPRI_"


class _Base(BaseModel):
    model_config = ConfigDict(extra="forbid")


# --------------------------------------------------------------------------- models


class ModelCfg(_Base):
    name: str
    ctx: int = Field(default=8192, ge=512)
    temperature: float = Field(default=0.7, ge=0.0, le=2.0)


class EmbedCfg(_Base):
    name: str = "bge-m3"
    device: Literal["cpu", "gpu"] = "cpu"
    dim: int = 1024


class RerankCfg(_Base):
    name: str = "bge-reranker-v2-m3"
    device: Literal["cpu", "gpu"] = "cpu"
    enabled: bool = True


class GovernorCfg(_Base):
    max_vram_gb: int = Field(default=14, ge=2, le=128)
    keep_planner_resident: bool = True


class ModelsCfg(_Base):
    endpoint: str = "http://127.0.0.1:8080/v1"
    planner: ModelCfg = Field(
        default_factory=lambda: ModelCfg(
            name="qwen3-14b-instruct-q4_k_m", ctx=16384, temperature=0.6
        )
    )
    reflex: ModelCfg = Field(
        default_factory=lambda: ModelCfg(name="qwen3-8b-q4_k_m", ctx=8192, temperature=0.8)
    )
    vision: ModelCfg = Field(
        default_factory=lambda: ModelCfg(name="qwen3-vl-4b-q4_k_m", ctx=8192, temperature=0.2)
    )
    embed: EmbedCfg = Field(default_factory=EmbedCfg)
    rerank: RerankCfg = Field(default_factory=RerankCfg)
    governor: GovernorCfg = Field(default_factory=GovernorCfg)


class AgentCfg(_Base):
    max_steps: int = Field(default=24, ge=1, le=200)
    max_wall_clock_s: int = Field(default=600, ge=10)
    context_budget_tokens: int = Field(default=12000, ge=1024)
    one_tool_per_turn: bool = True


class MemoryCfg(_Base):
    extract_after_turn: bool = True
    decay_halflife_days: int = Field(default=30, ge=1)
    max_semantic_items: int = Field(default=5000, ge=100)


class RagCfg(_Base):
    collections: list[str] = Field(default_factory=list)
    chunk_tokens: int = Field(default=700, ge=64)
    overlap_tokens: int = Field(default=80, ge=0)
    top_k: int = Field(default=40, ge=1, le=500)
    rerank_top_n: int = Field(default=8, ge=1, le=64)


class BlendCfg(_Base):
    a: str = "af_bella"
    b: str = "af_nicole"
    mix: float = Field(default=0.55, ge=0.0, le=1.0)


class DspCfg(_Base):
    pitch: float = Field(default=-1.5, ge=-12.0, le=12.0)
    formant: float = Field(default=1.04, ge=0.5, le=2.0)
    bitcrush: float = Field(default=0.12, ge=0.0, le=1.0)
    radio: float = Field(default=0.08, ge=0.0, le=1.0)


class VoiceCfg(_Base):
    tts: Literal["kokoro", "piper", "chatterbox"] = "kokoro"
    voice_blend: BlendCfg = Field(default_factory=BlendCfg)
    speed: float = Field(default=1.02, ge=0.5, le=2.0)
    dsp: DspCfg = Field(default_factory=DspCfg)
    stt: str = "faster-whisper-small-int8"
    barge_in: bool = True
    wake_word: str | None = None


class AvatarCfg(_Base):
    pack: str = "capri-placeholder"
    scale: float = Field(default=1.0, ge=0.5, le=2.0)
    always_on_top: bool = True
    click_through: bool = True
    edge_snap: bool = True
    mood_default: str = "neutral"


class ConfirmCfg(_Base):
    destructive: Literal["always", "never"] = "always"
    purchase: Literal["always", "block"] = "block"
    passwords: Literal["always", "block"] = "block"


class PolicyCfg(_Base):
    profile: Literal["focus", "work", "chatty", "silent"] = "focus"
    allow_paths: list[str] = Field(
        default_factory=lambda: [
            "C:/Users/daeme/projects",
            "C:/Users/daeme/Documents",
        ]
    )
    deny_paths: list[str] = Field(
        default_factory=lambda: [
            "C:/Windows",
            "C:/Users/daeme/.ssh",
            "**/*.kdbx",
            "**/AppData/Roaming/**",
        ]
    )
    allow_hosts: list[str] = Field(default_factory=list)
    screen_allow_apps: list[str] = Field(
        default_factory=lambda: ["chrome.exe", "code.exe", "explorer.exe", "notepad.exe"]
    )
    confirm: ConfirmCfg = Field(default_factory=ConfirmCfg)


class ServerCfg(_Base):
    host: str = "127.0.0.1"
    port: int = Field(default=8787, ge=1024, le=65535)
    require_token: bool = True


class CapriConfig(_Base):
    version: int = 1
    models: ModelsCfg = Field(default_factory=ModelsCfg)
    agent: AgentCfg = Field(default_factory=AgentCfg)
    memory: MemoryCfg = Field(default_factory=MemoryCfg)
    rag: RagCfg = Field(default_factory=RagCfg)
    voice: VoiceCfg = Field(default_factory=VoiceCfg)
    avatar: AvatarCfg = Field(default_factory=AvatarCfg)
    policy: PolicyCfg = Field(default_factory=PolicyCfg)
    server: ServerCfg = Field(default_factory=ServerCfg)


# --------------------------------------------------------------------------- loading


def deep_merge(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    out = copy.deepcopy(base)
    for key, value in (overlay or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def _read_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ValueError(f"{path} must contain a YAML mapping at the top level")
    return data


def _coerce(value: str) -> Any:
    low = value.lower()
    if low in ("true", "false"):
        return low == "true"
    if low in ("null", "none"):
        return None
    try:
        return int(value)
    except ValueError:
        pass
    try:
        return float(value)
    except ValueError:
        return value


def env_overrides() -> dict[str, Any]:
    """CAPRI_MODELS__ENDPOINT=http://... -> {"models": {"endpoint": ...}}"""
    out: dict[str, Any] = {}
    for key, value in os.environ.items():
        if not key.startswith(_ENV_PREFIX) or "__" not in key:
            continue
        parts = [p.lower() for p in key[len(_ENV_PREFIX) :].split("__") if p]
        if not parts:
            continue
        node = out
        for part in parts[:-1]:
            node = node.setdefault(part, {})
        node[parts[-1]] = _coerce(value)
    return out


def load_config(project_dir: Path | None = None) -> CapriConfig:
    data: dict[str, Any] = {}
    data = deep_merge(data, _read_yaml(paths.config_path()))
    if project_dir is not None:
        data = deep_merge(data, _read_yaml(project_dir / ".capri.yaml"))
    data = deep_merge(data, env_overrides())
    try:
        return CapriConfig.model_validate(data)
    except ValidationError as exc:  # surface the file, not just the field
        raise ValueError(f"invalid configuration:\n{exc}") from exc


def save_config(config: CapriConfig, path: Path | None = None) -> Path:
    target = path or paths.config_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(dump_yaml(config), encoding="utf-8")
    return target


def dump_yaml(config: CapriConfig) -> str:
    return yaml.safe_dump(
        config.model_dump(mode="json"), sort_keys=False, allow_unicode=True, width=100
    )


def schema() -> dict[str, Any]:
    """JSON Schema for the settings UI."""
    return CapriConfig.model_json_schema()


def set_value(config: CapriConfig, dotted: str, value: Any) -> CapriConfig:
    """Set ``a.b.c`` on a config copy, validating the result."""
    parts = dotted.split(".")
    if not parts or any(not p for p in parts):
        raise ValueError(f"invalid config path: {dotted!r}")
    data = config.model_dump(mode="json")
    node: dict[str, Any] = data
    for part in parts[:-1]:
        if part not in node or not isinstance(node[part], dict):
            raise KeyError(f"unknown config section: {part}")
        node = node[part]
    if parts[-1] not in node:
        raise KeyError(f"unknown config key: {dotted}")
    node[parts[-1]] = value
    return CapriConfig.model_validate(data)
