"""Character filter for the voice.

Bitcrush and a radio band live here, plus the two pieces the pitch shift needs:
``resample`` for the pitch itself and ``time_stretch`` (WSOLA) to put the
duration back without touching pitch. The engine therefore never changes its
tempo for pitch, so its own F0 contour stays where it was. The talk-box robot
lives in ``robot.py`` and is mixed in last but for the layers. Formant shifting
needs a phase vocoder and is not configurable here yet.
"""

from __future__ import annotations

import numpy as np

from ..config import DspCfg
from .robot import vocode


def _clip(samples: np.ndarray) -> np.ndarray:
    return np.clip(samples, -1.0, 1.0).astype(np.float32, copy=False)


def bitcrush(samples: np.ndarray, amount: float) -> np.ndarray:
    """Quantise amplitude and hold samples. ``amount`` is 0..1."""
    if amount <= 0 or samples.size == 0:
        return samples
    amount = min(1.0, float(amount))
    levels = max(2.0, 2.0 ** (16.0 - 10.0 * amount))
    crushed = np.round(samples * levels) / levels
    hold = 1 + int(amount * 4)
    if hold > 1:
        indices = (np.arange(crushed.size) // hold) * hold
        crushed = crushed[np.clip(indices, 0, crushed.size - 1)]
    return _clip(crushed)


def _one_pole_lowpass(samples: np.ndarray, cutoff_hz: float, rate: int) -> np.ndarray:
    if samples.size == 0 or cutoff_hz <= 0:
        return samples
    alpha = 1.0 - np.exp(-2.0 * np.pi * cutoff_hz / max(1, rate))
    out = np.empty_like(samples)
    state = 0.0
    for index, value in enumerate(samples):
        state += alpha * (float(value) - state)
        out[index] = state
    return out


def _one_pole_highpass(samples: np.ndarray, cutoff_hz: float, rate: int) -> np.ndarray:
    return samples - _one_pole_lowpass(samples, cutoff_hz, rate)


def radio(samples: np.ndarray, amount: float, rate: int) -> np.ndarray:
    """Narrow band plus mild saturation, mixed in by ``amount``."""
    if amount <= 0 or samples.size == 0:
        return samples
    amount = min(1.0, float(amount))
    band = _one_pole_highpass(samples, 300.0, rate)
    band = _one_pole_lowpass(band, 3_200.0, rate)
    band = np.tanh(band * 1.6) * 0.8
    return _clip(samples * (1.0 - amount) + band * amount)


def resample(samples: np.ndarray, factor: float, half_width: int = 12) -> np.ndarray:
    """Play ``samples`` ``factor`` times faster: shorter and higher by that ratio.

    Windowed-sinc interpolation, low-passed at the new Nyquist when the ratio
decimates, so a lift does not fold high frequencies back. Numpy only: the
    Kokoro path does not get to depend on scipy.
    """
    x = np.asarray(samples, dtype=np.float32)
    if x.size == 0 or abs(factor - 1.0) < 1e-9 or factor <= 0:
        return x

    factor = float(factor)
    out_len = max(1, int(round(x.size / factor)))
    cutoff = 0.5 if factor < 1.0 else 0.5 / factor
    offsets = np.arange(-half_width, half_width + 1, dtype=np.int64)
    padded = np.pad(x, half_width, mode="edge")
    out = np.empty(out_len, dtype=np.float32)

    # Chunked so a long sentence does not allocate the whole kernel matrix.
    block = 65_536
    for start in range(0, out_len, block):
        stop = min(out_len, start + block)
        positions = np.arange(start, stop, dtype=np.float64) * factor
        base = np.floor(positions)
        distance = offsets[None, :] - (positions - base)[:, None]
        window = np.where(
            np.abs(distance) <= half_width,
            0.5 + 0.5 * np.cos(np.pi * distance / half_width),
            0.0,
        )
        kernel = 2.0 * cutoff * np.sinc(2.0 * cutoff * distance) * window
        kernel /= np.sum(kernel, axis=1, keepdims=True)
        indices = np.clip(
            base[:, None].astype(np.int64) + offsets[None, :] + half_width,
            0,
            padded.size - 1,
        )
        out[start:stop] = np.sum(padded[indices] * kernel, axis=1)
    return _clip(out)


def time_stretch(
    samples: np.ndarray, factor: float, frame: int = 1024, tolerance: int = 200
) -> np.ndarray:
    """Change duration by ``factor`` (>1 longer) without changing pitch.

    WSOLA: overlap-add Hann windows, each new frame taken from the input
    position that best continues the previous frame's phase. Used to undo the
    shortening a pitch resample causes, so the tempo is not handed to the TTS
    model (changing that also moves its F0 contour).
    """
    x = np.asarray(samples, dtype=np.float32)
    if x.size == 0 or abs(factor - 1.0) < 1e-6 or factor <= 0:
        return x

    factor = float(factor)
    hop = frame // 2
    window = np.hanning(frame).astype(np.float32)
    out_len = max(1, int(round(x.size * factor)))
    out = np.zeros(out_len + frame, dtype=np.float32)
    norm = np.zeros_like(out)
    in_pos = 0.0
    out_pos = 0
    previous = None

    while out_pos + frame <= out.size:
        start = int(round(in_pos))
        start = min(max(0, start), max(0, x.size - frame))
        if previous is not None and x.size > frame:
            low = max(0, start - tolerance)
            high = max(low, min(x.size - frame, start + tolerance))
            # One gather and one matmul for every candidate, instead of a
            # Python loop per candidate: this is the hot path for the layers.
            offsets = np.arange(low, high + 1, 8)
            if offsets.size:
                candidates = x[offsets[:, None] + np.arange(hop)[None, :]]
                scores = candidates @ previous
                start = int(offsets[int(np.argmax(scores))])
        segment = x[start : start + frame]
        if segment.size == 0:
            break
        if segment.size < frame:
            segment = np.pad(segment, (0, frame - segment.size), mode="edge")
        out[out_pos : out_pos + frame] += segment * window
        norm[out_pos : out_pos + frame] += window
        previous = segment[-hop:].copy()
        out_pos += hop
        in_pos += hop / factor

    mask = norm > 1e-6
    out[mask] /= norm[mask]
    return _clip(out[:out_len])


_REPO_KERNELS: dict[float, tuple[np.ndarray, int]] = {}
_REPO_KERNEL_LIMIT = 32


def _repo_kernel(factor: float) -> tuple[np.ndarray, int]:
    """The 257-phase sinc kernel for one lift, built once and reused.

    The layers call this for the same two pitches on every sentence, so the
    table is cached instead of rebuilt per segment.
    """
    key = round(float(factor), 6)
    cached = _REPO_KERNELS.get(key)
    if cached is not None:
        return cached

    cutoff = min(1.0, 1.0 / key)
    reach = int(np.ceil(16.0 / cutoff))
    taps = 2 * reach
    phases = np.arange(257, dtype=np.float64)[:, None] / 256.0
    offsets = np.arange(taps, dtype=np.float64)[None, :] - reach + 1 - phases
    t = offsets * cutoff
    angle = np.pi * t

    sinc = np.ones_like(t)
    nonzero = np.abs(t) >= 1e-9
    sinc[nonzero] = np.sin(angle[nonzero]) / angle[nonzero]
    window = np.where(
        np.abs(offsets) >= reach,
        0.0,
        0.5 + 0.5 * np.cos(np.pi * offsets / reach),
    )
    kernel = sinc * window
    kernel /= np.sum(kernel, axis=1, keepdims=True)
    if len(_REPO_KERNELS) >= _REPO_KERNEL_LIMIT:
        _REPO_KERNELS.clear()
    _REPO_KERNELS[key] = (kernel, reach)
    return kernel, reach


def resample_repo(samples: np.ndarray, factor: float) -> np.ndarray:
    """capri-desktop's exact Kokoro resampler: 256 phases, 16-sample sinc.

    Its Windows voice converts the generated float buffer to 16-bit, then runs
    this kernel and writes the shorts. Reproducing that here keeps her pitch
    resample sample-for-sample with the repo instead of merely using a similar
    windowed-sinc filter. ``factor`` is the pitch lift, so the result is
    ``1/factor`` as long and ``factor`` higher.
    """
    x = np.asarray(samples, dtype=np.float32)
    if x.size == 0 or abs(factor - 1.0) < 1e-9 or factor <= 0:
        return x

    factor = float(factor)
    pcm = np.clip(np.rint(x.astype(np.float64) * 32767.0), -32768, 32767).astype(np.int16)
    kernel, reach = _repo_kernel(factor)
    taps = kernel.shape[1]

    count = int(np.rint(pcm.size / factor))
    out = np.empty(count, dtype=np.float32)
    block = 65_536
    for start in range(0, count, block):
        stop = min(count, start + block)
        centers = np.arange(start, stop, dtype=np.float64) * factor
        whole = np.floor(centers).astype(np.int64)
        phase = np.rint((centers - whole) * 256.0).astype(np.int64)
        indices = whole[:, None] - reach + 1 + np.arange(taps, dtype=np.int64)[None, :]
        valid = (indices >= 0) & (indices < pcm.size)
        gathered = pcm[np.clip(indices, 0, pcm.size - 1)] * valid
        mixed = np.sum(gathered.astype(np.float64) * kernel[phase], axis=1)
        out[start:stop] = np.clip(np.rint(mixed), -32768, 32767).astype(np.int16) / 32768.0
    return out


def pitch_shift(samples: np.ndarray, semitones: float) -> np.ndarray:
    """Shift by ``semitones`` and keep the duration: resample, then stretch back.

    Uses the cached phase-table resampler: the layers ask for the same two
    pitches on every sentence, and ``resample`` rebuilds a sinc kernel per
    output block, which made a layered segment four times more expensive.
    """
    lift = 2.0 ** (float(semitones) / 12.0)
    return time_stretch(resample_repo(samples, lift), lift)


def _delayed(samples: np.ndarray, delay_ms: int, rate: int) -> np.ndarray:
    shift = max(0, int(round(rate * float(delay_ms) / 1000.0)))
    if shift == 0:
        return samples
    return np.concatenate([np.zeros(shift, dtype=np.float32), samples])


def mix_layers(samples: np.ndarray, cfg: DspCfg, rate: int) -> np.ndarray:
    """Two pitched, delayed copies of the dry voice under it, at ``layer_level``.

    Each layer is pitch-shifted with the same resample/stretch pair the engine
    uses, so a layer does not change the length of the take. Mixed at 0.4 each,
    about 8 dB down, which reads as a doubled shimmer rather than a chord.
    """
    dry = np.asarray(samples, dtype=np.float32)
    out = dry.copy()
    for pitch, delay in (
        (cfg.layer_a_pitch, cfg.layer_a_delay),
        (cfg.layer_b_pitch, cfg.layer_b_delay),
    ):
        layer = _delayed(pitch_shift(dry, pitch), delay, rate)
        length = min(out.size, layer.size)
        out[:length] += float(cfg.layer_level) * layer[:length]
    return _clip(out)


def apply(samples: np.ndarray, cfg: DspCfg, rate: int) -> np.ndarray:
    out = np.asarray(samples, dtype=np.float32)
    out = bitcrush(out, cfg.bitcrush)
    out = radio(out, cfg.radio, rate)
    if cfg.robot > 0:
        out = vocode(out, cfg.robot, rate)
    if cfg.layers:
        out = mix_layers(out, cfg, rate)
    return _clip(out)
