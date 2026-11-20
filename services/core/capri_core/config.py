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


class EmbedCfg(_Base):
    endpoint: str = Field(
        default="",
        title="Embedding server URL",
        description=(
            "Optional separate endpoint for embeddings (e.g. http://127.0.0.1:8081/v1). "
            "Leave blank to use the main server URL."
        ),
    )
    name: str = Field(
        default="",
        title="Embedding model",
        description=(
            "Optional embedding model ID. Leave blank to use whatever model "
            "the server has loaded, or to use the hash fallback."
        ),
    )
    dim: int = Field(
        default=1024,
        ge=1,
        title="Embedding size",
        description="Vector size returned by the embedding model.",
    )


class RerankCfg(_Base):
    name: str = Field(
        default="",
        title="Rerank model",
        description="Optional rerank model ID; leave blank to use lexical reranking.",
    )
    enabled: bool = Field(
        default=True,
        title="Use reranking",
        description="Try the server's rerank endpoint; fall back to lexical scoring.",
    )


class ModelsCfg(_Base):
    endpoint: str = Field(
        default="",
        title="Server URL",
        description=(
            "OpenAI-compatible base URL, for example "
            "http://127.0.0.1:1234/v1 (LM Studio) or "
            "http://127.0.0.1:8080/v1 (llama.cpp)."
        ),
    )
    model: str = Field(
        default="",
        title="Model ID",
        description=(
            "The model identifier shown by your server's /v1/models endpoint. "
            "Use the same model for planning, replies, and background work."
        ),
    )
    decision_endpoint: str = Field(
        default="",
        title="Decision server URL",
        description=(
            "Optional separate endpoint for decision/fast routing calls. "
            "Leave blank to use the main server URL."
        ),
    )
    decision_model: str = Field(
        default="",
        title="Decision model ID",
        description=(
            "Optional model ID for decision/routing (e.g. a small fast model). "
            "Leave blank to use the main model."
        ),
    )
    # No sampling knobs: the server that runs the model owns them. A
    # temperature here was sent on every request, which split authority over
    # sampling between the harness and the endpoint.
    # Constrained decoding. `json_schema` is the portable OpenAI-compatible
    # response_format; `grammar` is llama.cpp's GBNF fallback. `none` leaves
    # it to the prompt.
    structured: Literal["grammar", "json_schema", "none"] = Field(
        default="json_schema",
        title="Structured output",
        description=(
            "JSON schema is the default; use GBNF if llama.cpp rejects "
            "response_format."
        ),
    )
    disable_thinking: bool = Field(
        default=False,
        title="Disable thinking",
        description=(
            "Ask llama.cpp to disable the model's thinking template. "
            "Useful for reasoning models that never finish their output."
        ),
    )
    # A reasoning model's thinking comes out of the same budget as its answer,
    # so a small cap truncates the JSON mid-object. Servers that enforce
    # structured output reject that outright (HTTP 502), which reads as the
    # model being down. This is the budget for one planner or tool-argument
    # call, not for a whole turn.
    max_output_tokens: int = Field(
        default=4096,
        ge=256,
        le=32768,
        title="Output budget (tokens)",
        description=(
            "Most a single planner or tool-argument call may generate, thinking "
            "included. Raise it if the server reports structured output was cut "
            "off (finish_reason=length)."
        ),
    )
    request_timeout_s: int = Field(
        default=180,
        ge=5,
        le=1800,
        title="Request timeout (seconds)",
    )
    health_ttl_s: int = Field(
        default=10,
        ge=1,
        le=120,
        title="Health check cache (seconds)",
    )
    embed: EmbedCfg = Field(default_factory=EmbedCfg)
    rerank: RerankCfg = Field(default_factory=RerankCfg)


class ObservationCfg(_Base):
    """How much of each tool result the planner gets to see, in characters.

    A flat limit cannot serve both a file the agent must patch exactly and a
    short error line. Shapes let a tool declare what it returns without
    hardcoding a number inside the tool.
    """

    default: int = Field(default=1200, ge=200, le=200_000)
    file: int = Field(default=8000, ge=200, le=200_000)
    command: int = Field(
        default=6000,
        ge=200,
        le=200_000,
        description="Head and tail of command output; a failing test reports at the end.",
    )
    grep: int = Field(default=4000, ge=200, le=200_000)
    tree: int = Field(default=2500, ge=200, le=200_000)
    summary: int = Field(default=1500, ge=200, le=200_000)
    screen: int = Field(default=3000, ge=200, le=200_000)


class AgentCfg(_Base):
    max_steps: int = Field(default=24, ge=1, le=200)
    # Ceiling on tool calls across the whole turn, independent of max_steps.
    max_tool_calls_per_turn: int = Field(default=40, ge=1, le=400)
    max_wall_clock_s: int = Field(default=600, ge=10)
    context_budget_tokens: int = Field(default=12000, ge=1024)
    # The work profile gets more room: a six-step coding task is mostly
    # observations. Effective budget is the minimum of this and the model's
    # usable window.
    work_context_budget_tokens: int = Field(default=24000, ge=1024)
    persona_pack: str = "kyoko"
    confirm_timeout_s: int = Field(default=180, ge=10, le=3600)
    history_messages: int = Field(default=30, ge=2, le=200)
    observation: ObservationCfg = Field(default_factory=ObservationCfg)


class MemoryCfg(_Base):
    extract_after_turn: bool = True
    decay_halflife_days: int = Field(default=30, ge=1)
    # "endpoint" uses /v1/embeddings on the model server; "hash" is a
    # deterministic local fallback for tests and offline work.
    embed: Literal["endpoint", "hash"] = "endpoint"
    recall_items: int = Field(default=8, ge=1, le=50)
    recall_budget_tokens: int = Field(default=800, ge=100, le=4000)
    dedupe_threshold: float = Field(default=0.92, ge=0.5, le=1.0)
    contradiction_threshold: float = Field(default=0.80, ge=0.5, le=1.0)
    prune_below: float = Field(default=0.05, ge=0.0, le=1.0)


class RagCfg(_Base):
    # Empty means "ingest into a collection called default" until the user
    # names one explicitly.
    collections: list[str] = Field(default_factory=list)
    chunk_tokens: int = Field(default=700, ge=64)
    overlap_tokens: int = Field(default=80, ge=0)
    top_k: int = Field(default=40, ge=1, le=500)
    rerank_top_n: int = Field(default=8, ge=1, le=64)
    # One cheap rewrite call before retrieval; multi_query adds up to 3 variants.
    rewrite: bool = True
    multi_query: bool = False
    context_items: int = Field(default=6, ge=1, le=32)
    context_budget_tokens: int = Field(default=1600, ge=200, le=8000)
    max_doc_bytes: int = Field(default=2_000_000, ge=10_000)


class NotesCfg(_Base):
    # The person's Obsidian vault, as a folder. Empty means "not set up yet":
    # the note tools say so rather than guessing a path, and nothing is indexed.
    vault: str = ""
    # Her wiki, not just a folder: with this on and a vault set, every turn
    # starts with the notes that match the request.
    recall: bool = True


class BlendCfg(_Base):
    a: str = "af_bella"


class DspCfg(_Base):
    # Semitones. capri-desktop tunes her to +2 (their release notes: "She
    # starts at pitch +2"), so that is where she starts here too: neutral reads
    # as a different, lower voice. KokoroEngine compensates its tempo by this
    # lift and then resamples; the other engines ignore it for now.
    pitch: float = Field(default=2.0, ge=-12.0, le=12.0)
    bitcrush: float = Field(default=0.0, ge=0.0, le=1.0)
    radio: float = Field(default=0.0, ge=0.0, le=1.0)
    # Her robot: a sawtooth carrier on her nearest note with her spectral
    # envelope, mixed under her at equal power. 0 is off, 1 is all robot.
    robot: float = Field(default=0.0, ge=0.0, le=1.0)
    # Her voice layers, as capri-desktop has them: two copies of the finished
    # voice, each pitched on its own and delayed, mixed under the dry signal.
    # On by default: their preset leaves it opt-in, but the doubled voice is
    # the one she is supposed to sound like. Level 0.4 is about 8 dB down.
    layers: bool = True
    layer_a_pitch: float = Field(default=2.0, ge=-12.0, le=12.0)
    layer_a_delay: int = Field(default=25, ge=0, le=250)
    layer_b_pitch: float = Field(default=-2.5, ge=-12.0, le=12.0)
    layer_b_delay: int = Field(default=25, ge=0, le=250)
    layer_level: float = Field(default=0.4, ge=0.0, le=1.0)


class WakeCfg(_Base):
    """Her name as the microphone's gate.

    A wake that carries no request ("Capri?") opens one follow-up window
    instead. The capture is the renderer's, so the timings live here where the
    settings UI can reach them.
    """

    enabled: bool = True
    # A wake candidate the recogniser is unsure about is not her name: a cough,
    # the TV, or someone else's sentence should never become a request. Both
    # limits are applied before the name is even matched.
    min_confidence: float = Field(
        default=0.35,
        ge=0.0,
        le=1.0,
        title="Wake confidence",
        description=(
            "How sure the recogniser must be before an utterance counts as a "
            "wake candidate. Raise it if she reacts to noise, lower it if she "
            "ignores you in a noisy room."
        ),
    )
    max_no_speech_prob: float = Field(
        default=0.6,
        ge=0.0,
        le=1.0,
        title="Reject silence",
        description=(
            "Reject a wake candidate the recogniser thinks contains no speech "
            "at all, above this probability."
        ),
    )
    # Silence that ends a captured utterance: short enough that the end of a
    # sentence is noticed at once. A short opening is given the grace below, so
    # "Capri" followed by a pause is not cut off from its request.
    silence_ms: int = Field(default=550, ge=200, le=3000)
    # Extra silence allowed when too little has been said for the utterance to
    # be a sentence: it may be only her name, and the request is on its way.
    name_grace_ms: int = Field(
        default=700,
        ge=0,
        le=2000,
        title="Name grace (ms)",
        description=(
            "Extra silence allowed after a very short utterance, so a pause "
            "after “Capri” does not cut the request off from its name."
        ),
    )
    # `voice.calibrate` measures these two against the room and the voice; the
    # defaults are what a quiet desk and a close microphone need.
    threshold_rms: float = Field(
        default=0.012,
        ge=0.001,
        le=0.2,
        title="Speech threshold",
        description=(
            "How loud the microphone has to be before the gate opens. "
            "Calibration puts this between the room's noise floor and your voice."
        ),
    )
    min_utterance_ms: int = Field(
        default=300,
        ge=100,
        le=1500,
        title="Shortest utterance",
        description=(
            "Sounds shorter than this never become a wake candidate. "
            "Calibration sets it from how long your \"Capri\" actually takes."
        ),
    )
    # A bare name, and every command that follows it, leave this long for the
    # sentence after it — the window that keeps "Capri" from being repeated.
    follow_up_ms: int = Field(
        default=6000,
        ge=1000,
        le=20000,
        title="Follow-up window",
        description=(
            "How long the microphone stays open for your next sentence after her "
            "name, and after each command, without saying “Capri” again."
        ),
    )


class VoiceCfg(_Base):
    enabled: bool = True
    # "tone" is the offline placeholder: same pipeline, no weights. It never
    # substitutes itself for a missing Kokoro install.
    tts: Literal["kokoro", "piper", "chatterbox", "pocket", "tone"] = "kokoro"
    # Empty means <capri home>/models/kokoro.
    model_dir: str = ""
    # Chatterbox clone target. Empty means <capri home>/models/chatterbox/capri-reference.wav.
    # Only ever a clip the user has the rights to; `--voice-reference` renders
    # Capri's own from the synthetic Kokoro blend.
    reference_clip: str = ""
    # Python that runs Chatterbox when it cannot live in this environment
    # (it pins numpy<2). Empty means <capri home>/models/chatterbox/venv.
    chatterbox_python: str = ""
    # Where the clone runs: auto (a CUDA GPU if this torch build has one, else
    # the CPU), cpu, or cuda. There is no GPU path for an AMD card on Windows
    # with the pinned torch, so auto is the CPU there.
    chatterbox_device: Literal["auto", "cpu", "cuda"] = "auto"
    # Pocket voice state. Empty means <capri home>/models/pocket/capri_english.safetensors.
    voice_file: str = ""
    # Piper .onnx voice. Empty means <capri home>/models/piper/en_US-lessac-medium.onnx.
    piper_voice: str = ""
    # Two-letter code for the Pocket language table (en, es, fr, de, it, pt, nl).
    language: str = "en"
    voice_blend: BlendCfg = Field(default_factory=BlendCfg)
    speed: float = Field(default=1.2, ge=0.5, le=2.0)
    dsp: DspCfg = Field(default_factory=DspCfg)
    # Finished segments held ahead of the consumer.
    queue_depth: int = Field(default=2, ge=1, le=8)
    sentence_max_chars: int = Field(default=240, ge=40, le=1000)
    viseme_fps: int = Field(default=15, ge=5, le=30)
    stt: str = "faster-whisper-small-int8"
    # Empty means <capri home>/models/whisper. The model downloads on first use.
    stt_model_dir: str = ""
    stt_compute_type: Literal["int8", "int8_float16", "float16", "float32"] = "int8"
    barge_in: bool = True
    # Renderer energy gate for barge-in: RMS threshold and how long it must hold.
    barge_threshold: float = Field(default=0.018, ge=0.002, le=0.2)
    barge_hold_ms: int = Field(default=120, ge=40, le=800)
    wake: WakeCfg = Field(default_factory=WakeCfg)


class AvatarCfg(_Base):
    always_on_top: bool = True
    edge_snap: bool = True


class ScreenCfg(_Base):
    enabled: bool = True
    # How long a screen action waits for the UIA tree to stop changing.
    settle_ms: int = Field(default=3000, ge=200, le=10_000)
    max_elements: int = Field(default=300, ge=20, le=2000)
    max_depth: int = Field(default=8, ge=1, le=20)
    capture: bool = True
    redact_passwords: bool = True
    # A visual second signal when the UIA tree does not change (canvas/game).
    pixel_diff: bool = True
    pixel_threshold: float = Field(default=0.002, ge=0.0, le=0.5)
    # Extra attempts for an idempotent action whose expected change never arrived.
    retries: int = Field(default=2, ge=0, le=4)
    # Windows OCR language tag (the OS language pack must be installed).
    ocr_language: str = "en"
    vision: Literal["auto", "on", "off"] = Field(
        default="auto",
        title="Visual context",
        description=(
            "auto: attach the redacted screenshot only when the model name is "
            "known to see images. on: always attach. off: never attach; the "
            "accessibility tree and OCR text still describe the window."
        ),
    )


class ConfirmCfg(_Base):
    destructive: Literal["always", "never"] = "always"
    # auto: a reviewer model may skip the question for a destructive call it
    # judges safe and undoable. off: every destructive call asks.
    review: Literal["auto", "off"] = "auto"
    review_timeout_s: float = Field(default=6.0, ge=1.0, le=30.0)


def _home_path(*parts: str) -> str:
    return str(Path.home().joinpath(*parts))


def _roaming_appdata() -> str:
    """``%APPDATA%``, or the same place built from the home directory."""
    raw = os.environ.get("APPDATA")
    if raw:
        return raw
    return _home_path("AppData", "Roaming")


def _default_allow_paths() -> list[str]:
    """The ambient roots: where a user's projects and documents live."""
    return [_home_path("projects"), _home_path("Documents")]


def _default_deny_paths() -> list[str]:
    """What the harness never reads, derived from the current user.

    These must follow the machine: a deny list built from one user's home
    protects nothing on anyone else's.
    """
    deny = [
        _roaming_appdata(),
        str(paths.capri_home()),
        _home_path(".ssh"),
        _home_path(".aws"),
        _home_path(".kube"),
        "**/*.kdbx",
    ]
    system_root = os.environ.get("SystemRoot") if os.name == "nt" else ""
    if system_root:
        deny.insert(0, system_root)
    return deny


class McpServerCfg(_Base):
    """One MCP server: a command to launch, and how much to trust it."""

    command: str = Field(default="", description="Executable to launch, e.g. npx or uvx.")
    args: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)
    cwd: str = ""
    enabled: bool = True
    # Everything a server offers confirms by default: the daemon cannot know
    # what a third-party tool does to the machine.
    tier: int = Field(default=2, ge=0, le=3)
    timeout_s: float = Field(default=30.0, ge=1.0, le=300.0)
    max_calls_per_turn: int = Field(default=4, ge=1, le=50)


class McpCfg(_Base):
    """Model Context Protocol servers whose tools join the registry."""

    enabled: bool = True
    startup_timeout_s: float = Field(default=15.0, ge=1.0, le=120.0)
    servers: dict[str, McpServerCfg] = Field(default_factory=dict)


class PolicyCfg(_Base):
    profile: Literal["focus", "work", "chatty", "silent"] = "focus"
    # Relative tool paths resolve here. Empty means the daemon's cwd.
    workspace: str = ""
    allow_paths: list[str] = Field(default_factory=_default_allow_paths)
    deny_paths: list[str] = Field(default_factory=_default_deny_paths)
    screen_allow_apps: list[str] = Field(
        default_factory=lambda: ["chrome.exe", "code.exe", "explorer.exe", "notepad.exe"]
    )
    workspace_edits: Literal["allow", "confirm"] = Field(
        default="allow",
        title="Workspace edits",
        description=(
            "allow: writes inside the open project run without a prompt. "
            "confirm: every write inside it asks first."
        ),
    )
    trust_persisted_workspace: bool = Field(
        default=True,
        title="Trust a reopened project",
        description=(
            "Keep write trust when a project is restored after a restart. "
            "False reopens it read-only until you open it again."
        ),
    )
    confirm: ConfirmCfg = Field(default_factory=ConfirmCfg)


class SchedulerCfg(_Base):
    # Idle maintenance only runs after this many seconds without a turn.
    enabled: bool = True
    interval_s: int = Field(default=900, ge=30, le=86_400)
    idle_s: int = Field(default=120, ge=10, le=86_400)
    journal_episodes: bool = True
    # Zero keeps the corresponding rows forever.
    episode_retention_days: int = Field(default=90, ge=0, le=3650)
    trace_retention_days: int = Field(default=30, ge=0, le=3650)


class CompanionCfg(_Base):
    """Ambient commentary about the foreground activity."""

    enabled: bool = True
    # The first line lands quickly; later ones are deliberately spaced out.
    initial_s: int = Field(default=45, ge=10, le=3600)
    interval_s: int = Field(default=180, ge=30, le=86_400)
    # A comment needs recent keyboard or mouse input, not just an open window.
    active_within_s: int = Field(default=120, ge=10, le=86_400)
    # A long session in one window still gets an occasional check-in, just
    # much less often than a change of activity does.
    repeat_s: int = Field(default=900, ge=60, le=86_400)
    # A hard ceiling so a burst of window switching cannot turn her into a
    # stream of notifications.
    max_per_hour: int = Field(default=8, ge=1, le=60)
    speak: bool = True
    # She may also start a conversation of her own, not only comment on what is
    # on the screen. This is the switch for that; the cadence is irregular.
    chatter: bool = Field(
        default=True,
        title="Talk to me",
        description=(
            "When on, she sometimes starts a conversation of her own - a question "
            "or a thought aimed at you - not only a line about the window."
        ),
    )
    # Base gap between conversations; each one is jittered, so it never lands
    # on a schedule.
    chatter_interval_s: int = Field(default=900, ge=120, le=86_400)
    # While a task is running she may think out loud about the step she is on.
    # This is narration, not the answer: no tool, no claim, no result.
    work_chatter: bool = Field(
        default=True,
        title="Comment on her own work",
        description=(
            "When on, she says a short line now and then while a task runs, and "
            "one when a scheduled maintenance pass finishes."
        ),
    )
    # One line, then quiet for this long: a long turn is not a monologue.
    work_interval_s: int = Field(default=15, ge=5, le=3600)
    # A hard ceiling per turn, so a forty-step turn still sounds like a companion.
    work_max_per_turn: int = Field(default=5, ge=1, le=10)
    # How much of the work is allowed into an ambient line. windows reads the
    # foreground title only; screen also lets the OCR of that window in.
    awareness: Literal["off", "windows", "screen"] = Field(
        default="windows",
        title="Awareness",
        description=(
            "off: no ambient commentary. windows: foreground app and title plus "
            "the open project and task. screen: also reads the text on screen "
            "so a line can refer to it (password regions are redacted)."
        ),
    )


class ServerCfg(_Base):
    host: str = "127.0.0.1"
    port: int = Field(default=8787, ge=1024, le=65535)
    require_token: bool = True


class CapriConfig(_Base):
    models: ModelsCfg = Field(
        default_factory=ModelsCfg,
        description="The local LM Studio or llama.cpp server that powers the brain.",
    )
    agent: AgentCfg = Field(
        default_factory=AgentCfg,
        description="Turn limits, context budget, and the active persona.",
    )
    memory: MemoryCfg = Field(
        default_factory=MemoryCfg,
        description="How Capri stores, recalls, and forgets what she learns.",
    )
    rag: RagCfg = Field(
        default_factory=RagCfg,
        description="Document indexing, retrieval, and citation behavior.",
    )
    notes: NotesCfg = Field(
        default_factory=NotesCfg,
        description="The Obsidian vault Capri reads and writes notes in.",
    )
    voice: VoiceCfg = Field(
        default_factory=VoiceCfg,
        description="Text-to-speech, speech-to-text, and voice effects.",
    )
    avatar: AvatarCfg = Field(
        default_factory=AvatarCfg,
        description="Sprite pack, scale, and window behavior.",
    )
    screen: ScreenCfg = Field(
        default_factory=ScreenCfg,
        description="Desktop observation, UI actions, and pixel verification.",
    )
    policy: PolicyCfg = Field(
        default_factory=PolicyCfg,
        description="Tool permissions, workspace paths, and confirmation rules.",
    )
    mcp: McpCfg = Field(
        default_factory=McpCfg,
        description="MCP servers whose tools Capri can call.",
    )
    scheduler: SchedulerCfg = Field(
        default_factory=SchedulerCfg,
        description="Idle maintenance, journaling, and retention.",
    )
    companion: CompanionCfg = Field(
        default_factory=CompanionCfg,
        description="Ambient comments about the activity on screen.",
    )
    server: ServerCfg = Field(
        default_factory=ServerCfg,
        description="Loopback daemon address and local authentication.",
    )


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


_OLD_MODEL_PRESETS = {
    "qwen3-14b-instruct-q4_k_m",
    "qwen3-8b-q4_k_m",
    "qwen3-vl-4b-q4_k_m",
}
_OLD_EMBED_PRESETS = {"bge-m3"}
_OLD_RERANK_PRESETS = {"bge-reranker-v2-m3"}


def _migrate_model_config(data: dict[str, Any]) -> dict[str, Any]:
    """Collapse role presets into one user-supplied brain model.

    Older configs had planner/reflex/vision objects with baked-in Qwen model
    names. Those names are intentionally not carried forward: the user now
    chooses the model in LM Studio or llama.cpp. A non-preset custom planner or
    reflex name is preserved so an explicit choice is not lost.
    """
    out = copy.deepcopy(data)
    models = out.get("models")
    if not isinstance(models, dict):
        return out

    old_planner = models.pop("planner", None)
    old_reflex = models.pop("reflex", None)
    models.pop("vision", None)
    models.pop("governor", None)

    if not models.get("model"):
        candidates: list[str] = []
        for old in (old_planner, old_reflex):
            if not isinstance(old, dict):
                continue
            name = str(old.get("name") or "").strip()
            if name and name not in _OLD_MODEL_PRESETS:
                candidates.append(name)
        if candidates:
            models["model"] = candidates[0]

    embed = models.get("embed")
    if isinstance(embed, dict):
        embed.pop("device", None)
        if str(embed.get("name") or "") in _OLD_EMBED_PRESETS:
            embed["name"] = ""

    rerank = models.get("rerank")
    if isinstance(rerank, dict):
        rerank.pop("device", None)
        if str(rerank.get("name") or "") in _OLD_RERANK_PRESETS:
            rerank["name"] = ""

    return out


def _drop_retired_config(data: dict[str, Any]) -> dict[str, Any]:
    """Remove fields that no longer have a runtime consumer.

    Existing config files are full dumps, so accepting an old file must not
    fail just because a retired switch is still present. These keys are dropped
    rather than silently kept in the schema where the settings UI would show a
    control that does nothing.
    """
    out = copy.deepcopy(data)
    out.pop("version", None)

    models = out.get("models")
    if isinstance(models, dict):
        # Sampling belongs to the server. A saved temperature is dropped rather
        # than carried into a schema that no longer has it.
        models.pop("temperature", None)

    agent = out.get("agent")
    if isinstance(agent, dict):
        agent.pop("one_tool_per_turn", None)

    memory = out.get("memory")
    if isinstance(memory, dict):
        memory.pop("max_semantic_items", None)

    voice = out.get("voice")
    if isinstance(voice, dict):
        voice.pop("wake_word", None)
        blend = voice.get("voice_blend")
        if isinstance(blend, dict):
            blend.pop("b", None)
            blend.pop("mix", None)
        dsp = voice.get("dsp")
        if isinstance(dsp, dict):
            dsp.pop("formant", None)

    avatar = out.get("avatar")
    if isinstance(avatar, dict):
        avatar.pop("pack", None)
        avatar.pop("scale", None)
        avatar.pop("mood_default", None)
        # Click-through is the pet window's own hit region now, not a setting.
        avatar.pop("click_through", None)

    policy = out.get("policy")
    if isinstance(policy, dict):
        policy.pop("allow_hosts", None)
        confirm = policy.get("confirm")
        if isinstance(confirm, dict):
            confirm.pop("purchase", None)
            confirm.pop("passwords", None)

    return out


_PROJECT_CONFIG_SECTIONS = frozenset({"agent", "rag", "companion", "avatar"})


def load_config(project_dir: Path | None = None) -> CapriConfig:
    data: dict[str, Any] = {}
    data = deep_merge(data, _read_yaml(paths.config_path()))
    if project_dir is not None:
        # Project configs cannot weaken policy, bind the server, or register
        # auto-started MCP servers/executables.
        project_overlay = {
            key: value
            for key, value in _read_yaml(project_dir / ".capri.yaml").items()
            if key in _PROJECT_CONFIG_SECTIONS
        }
        data = deep_merge(data, project_overlay)
    data = deep_merge(data, env_overrides())
    data = _drop_retired_config(_migrate_model_config(data))
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
