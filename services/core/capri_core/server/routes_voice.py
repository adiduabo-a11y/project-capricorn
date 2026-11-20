"""Voice TTS, STT, audio decoding, and mic route handlers."""

from __future__ import annotations

import asyncio
import base64
import binascii
import logging
import uuid
from typing import Any

from ..store import db
from ..voice import SttUnavailable
from .rpc import Client, RpcError

log = logging.getLogger("capri.server")


class VoiceRoutesMixin:
    """Voice synthesis, speech recognition, and mic route handlers."""

    async def _voice_speak(self, client: Client, params: dict[str, Any]) -> dict[str, Any]:
        self._require_session(client)
        text = str(params.get("text") or "").strip()
        if not text:
            raise RpcError("invalid_args", "text is required")
        turn_id = str(params.get("turnId") or f"voice_{uuid.uuid4().hex[:10]}")
        # A turn's reply preempts whatever is speaking; a cue or an
        # acknowledgement must not. The shell sends `preempt: false` for those,
        # because one that lands between `agent.done` and the first segment used
        # to cut the answer off mid-sentence.
        preempt = bool(params.get("preempt", True))
        # Do not await the whole stream in the client's dispatcher: that would
        # queue a following voice.barge_in behind every sentence. The turn
        # loop calls VoiceService directly; this RPC path is for the shell and
        # needs to remain interruptible.
        task = asyncio.create_task(
            self.voice.speak(turn_id, text, client.send, preempt=preempt)
        )
        self._voice_tasks.add(task)
        task.add_done_callback(self._voice_done)
        return {"started": True, "turnId": turn_id}

    def _voice_done(self, task: asyncio.Task[Any]) -> None:
        self._voice_tasks.discard(task)
        if task.cancelled():
            return
        error = task.exception()
        if error is not None:
            log.warning("voice.speak stream ended with an error: %s", error)

    @staticmethod
    def _decode_audio(raw: Any) -> bytes:
        """One WAV the renderer sends: a data URL or bare base64."""
        if not isinstance(raw, str) or not raw.strip():
            raise RpcError("invalid_args", "audio is required")
        encoded = raw.split(",", 1)[1] if raw.startswith("data:") and "," in raw else raw
        try:
            audio = base64.b64decode(encoded, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise RpcError("invalid_args", "audio must be base64 WAV") from exc
        if len(audio) > 12 * 1024 * 1024:
            raise RpcError("invalid_args", "recording is too large")
        return audio

    @classmethod
    def _audio_bytes(cls, params: dict[str, Any]) -> bytes:
        return cls._decode_audio(params.get("audio") or params.get("audioUrl") or params.get("data"))

    @classmethod
    def _audio_list(cls, params: dict[str, Any], key: str) -> list[bytes]:
        """One WAV or a short list of them, for the calibration takes."""
        raw = params.get(key)
        items = raw if isinstance(raw, list) else [raw]
        if not items or len(items) > 8 or any(not isinstance(item, str) for item in items):
            raise RpcError("invalid_args", f"{key} needs one to eight WAV recordings")
        return [cls._decode_audio(item) for item in items]

    async def _voice_listen(self, client: Client, params: dict[str, Any]) -> dict[str, Any]:
        self._require_session(client)
        audio = self._audio_bytes(params)
        language = str(params.get("language") or "").strip() or None
        try:
            return await self.voice.listen(audio, language=language)
        except SttUnavailable as exc:
            raise RpcError("voice_unavailable", str(exc)) from exc

    async def _voice_warm(self, client: Client, params: dict[str, Any]) -> dict[str, Any]:
        """The wake gate is armed: load the weights before anyone says her name."""
        self._require_session(client)
        try:
            return await self.voice.warm()
        except SttUnavailable as exc:
            raise RpcError("voice_unavailable", str(exc)) from exc

    async def _voice_calibrate(self, client: Client, params: dict[str, Any]) -> dict[str, Any]:
        """Measure this room and this voice; the shell writes the result to config."""
        self._require_session(client)
        noise = self._audio_list(params, "noise")
        speech = self._audio_list(params, "speech")
        try:
            payload = await self.voice.calibrate(noise, speech)
        except SttUnavailable as exc:
            raise RpcError("invalid_args", str(exc)) from exc
        db.trace(
            self.conn,
            span="voice",
            name="voice.calibrate",
            payload={
                "thresholdRms": payload["thresholdRms"],
                "minUtteranceMs": payload["minUtteranceMs"],
                "noiseFloor": payload["noiseFloor"],
                "speechLevel": payload["speechLevel"],
                "bargeThreshold": payload["bargeThreshold"],
                "minConfidence": payload.get("minConfidence"),
                "takes": len(payload["takesMs"]),
            },
        )
        return payload

    async def _voice_wake(self, client: Client, params: dict[str, Any]) -> dict[str, Any]:
        self._require_session(client)
        audio = self._audio_bytes(params)
        language = str(params.get("language") or "").strip() or None
        try:
            return await self.voice.wake(audio, language=language)
        except SttUnavailable as exc:
            raise RpcError("voice_unavailable", str(exc)) from exc

    async def _voice_follow_up(self, client: Client, params: dict[str, Any]) -> dict[str, Any]:
        """A sentence in an open follow-up window: no name, same gate."""
        self._require_session(client)
        audio = self._audio_bytes(params)
        language = str(params.get("language") or "").strip() or None
        try:
            return await self.voice.follow_up(audio, language=language)
        except SttUnavailable as exc:
            raise RpcError("voice_unavailable", str(exc)) from exc

    async def _voice_stop(self, client: Client, params: dict[str, Any]) -> dict[str, Any]:
        self._require_session(client)
        reason = str(params.get("reason") or "cancel")
        await self.voice.stop(reason)
        return {"stopped": True, "reason": reason}

    async def _voice_barge_in(self, client: Client, params: dict[str, Any]) -> dict[str, Any]:
        self._require_session(client)
        return {"stopped": await self.voice.barge_in()}

    async def _mic_route(self, client: Client, params: dict[str, Any]) -> dict[str, Any]:
        """Remember whether this shell is mixing Capri into a call mic."""
        active = bool(params.get("active"))
        client.mic_route_active = active
        stopped = await self.companion.silence() if active else False
        return {"active": active, "stopped": stopped}

    async def _voice_status(self, client: Client, params: dict[str, Any]) -> dict[str, Any]:
        self._require_session(client)
        return self.voice.status()

    async def _voice_download(self, client: Client, params: dict[str, Any]) -> dict[str, Any]:
        self._require_session(client)
        engine_name = str(params.get("engine") or "").strip() or None
        return await self.voice.download_models(engine_name)
