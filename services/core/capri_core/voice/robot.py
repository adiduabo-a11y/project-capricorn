"""Her robot: a talk-box vocoder.

A sawtooth carrier sings the note nearest her voice, gliding to each new one,
and takes her smoothed spectral envelope. Unvoiced frames get noise with the
same shape, so her s and t sounds still come through. The dry voice and the
robot mix at equal power: ``amount`` 0 is her, 1 is all robot.

Ported from capri-desktop's Vocoder, which is the effect behind its Robot
slider: about 43 ms frames a quarter frame apart, YIN pitch, a saw whose
harmonics are tilted flat, and a cepstral envelope smoothed over 2 ms.
"""

from __future__ import annotations

import numpy as np


def _pitch(frame: np.ndarray, rate: int, least: int, most: int) -> tuple[float, float]:
    """YIN over one frame: pitch in Hz and a 0..1 clarity."""
    size = frame.size
    smooth = 0.5 * frame + 0.25 * np.concatenate((frame[:1], frame[:-1])) + 0.25 * np.concatenate(
        (frame[1:], frame[-1:])
    )
    span = size // 2
    difference = np.ones(most + 2)
    running = 0.0
    for lag in range(1, most + 2):
        start = (size - span - lag) // 2
        delta = smooth[start : start + span] - smooth[start + lag : start + lag + span]
        total = float(np.dot(delta, delta))
        running += total
        difference[lag] = total * lag / running if running > 0 else 1.0

    best = -1
    for lag in range(least, most + 1):
        if difference[lag] < 0.15:
            while lag < most and difference[lag + 1] < difference[lag]:
                lag += 1
            best = lag
            break
    if best < 0:
        best = least + int(np.argmin(difference[least : most + 1]))
    dip = difference[best]
    if dip >= 0.35:
        return 0.0, 0.0
    before, after = difference[best - 1], difference[best + 1]
    bend = before - 2 * dip + after
    shift = 0.5 * (before - after) / bend if bend > 1e-12 else 0.0
    lag = best + max(-1.0, min(1.0, shift))
    pitch = rate / lag
    clarity = max(0.0, min(1.0, (0.35 - dip) / 0.2))
    return pitch, clarity


def _sawtooth(targets: np.ndarray, rate: int, lead: int, hop: int, pad: int, length: int) -> np.ndarray:
    """One band-limited saw sample at a time, gliding to each frame's note."""
    glide_step = 1 - np.exp(-1 / (0.015 * rate))
    carrier = np.zeros(length)
    phase = 0.0
    glide = 0.0
    sung = False
    frames = targets.size
    for index in range(length):
        frame = (index - pad + lead + hop // 2) // hop
        frame = max(0, min(frames - 1, frame))
        target = targets[frame]
        if not np.isnan(target):
            if not sung:
                glide = float(target)
                sung = True
            glide += (target - glide) * glide_step
        frequency = 440.0 * 2.0 ** ((glide - 69.0) / 12.0) if sung else 220.0
        increment = min(0.45, frequency / rate)
        phase += increment
        if phase >= 1:
            phase -= 1
        edge = 0.0
        if phase < increment:
            t = phase / increment
            edge = t + t - t * t - 1
        elif phase > 1 - increment:
            t = (phase - 1) / increment
            edge = t * t + t + t + 1
        carrier[index] = 2 * phase - 1 - edge
    return carrier


def vocode(samples: np.ndarray, amount: float, rate: int, seed: int = 1) -> np.ndarray:
    """Her voice through the robot at ``amount`` (0..1), same length."""
    dry_samples = np.asarray(samples, dtype=np.float64).reshape(-1)
    amount = max(0.0, min(1.0, float(amount)))
    if dry_samples.size == 0 or amount <= 0:
        return np.asarray(samples, dtype=np.float32)

    size = 2048 if rate >= 32_000 else 1024 if rate >= 20_000 else 512
    hop = size // 4
    half = size // 2
    lifter = max(8, int(round(rate * 0.002)))
    reach = max(1, int(round(150.0 * size / rate)))
    least = max(2, rate // 900)
    most = min(half - 1, rate // 60)
    lead = int(round(0.015 * rate))

    window = 0.5 - 0.5 * np.cos(2 * np.pi * np.arange(size) / size)
    frequencies = np.arange(half + 1) * rate / size
    tilt = np.arange(half + 1) / np.sqrt(1 + (frequencies / 5000.0) ** 2)

    pad = size // 2
    padded = np.pad(dry_samples, (pad, pad))
    frames = (padded.size - size) // hop + 1

    levels = np.zeros(frames)
    for frame in range(frames):
        window_frame = padded[frame * hop : frame * hop + size]
        middle = window_frame[size // 4 : size * 3 // 4]
        levels[frame] = float(np.sqrt(np.mean(middle * middle)))
    floor = max(20 / 32768, 0.03 * float(np.max(levels)))

    pitches = np.zeros(frames)
    clarities = np.zeros(frames)
    for frame in range(frames):
        if levels[frame] > floor:
            window_frame = padded[frame * hop : frame * hop + size]
            pitches[frame], clarities[frame] = _pitch(window_frame, rate, least, most)

    snap = min(1.0, 0.5 + 0.5 * amount)
    targets = np.full(frames, np.nan)
    note = np.nan
    for frame in range(frames):
        pitch = pitches[frame]
        if pitch <= 0:
            continue
        around = [pitches[index] for index in range(frame - 2, frame + 3) if 0 <= index < frames and pitches[index] > 0]
        if len(around) >= 3:
            pitch = float(np.median(around))
        semitone = 69 + 12 * np.log2(pitch / 440.0)
        if np.isnan(note) or abs(semitone - note) > 0.65:
            note = float(np.round(semitone))
        targets[frame] = semitone + snap * (note - semitone)

    carrier = _sawtooth(targets, rate, lead, hop, pad, padded.size)

    voiced = 0.0
    voice_step = 1 - np.exp(-hop / (0.01 * rate))
    voicing = np.zeros(frames)
    for frame in range(frames):
        voiced += (clarities[frame] - voiced) * voice_step
        voicing[frame] = voiced

    spectrum_sum = np.zeros(padded.size)
    weights = np.zeros(padded.size)
    for frame in range(frames):
        start = frame * hop
        voice_frame = padded[start : start + size]
        power = np.abs(np.fft.rfft(voice_frame * window)) ** 2
        energy = power[0] + power[-1] + 2 * float(np.sum(power[1:-1]))

        # Her power averaged over about 300 Hz either side, then log and
        # liftered, so the shape follows formants rather than harmonics.
        cumulative = np.concatenate(([0.0], np.cumsum(power)))
        bins = np.arange(half + 1)
        low = np.maximum(0, bins - reach)
        high = np.minimum(half, bins + reach)
        envelope = (cumulative[high + 1] - cumulative[low]) / (high - low + 1)
        log_shape = 0.5 * np.log(envelope + 1e-2)
        cepstrum = np.fft.irfft(log_shape, n=size)
        distance = np.minimum(np.arange(size), size - np.arange(size))
        cepstrum *= np.where(
            distance <= lifter,
            0.5 + 0.5 * np.cos(np.pi * distance / (lifter + 1)),
            0.0,
        )
        envelope = np.exp(np.fft.rfft(cepstrum).real)

        saw = np.fft.rfft(carrier[start : start + size] * window)
        shaped = saw * envelope * tilt
        saw_energy = 2 * float(np.sum(np.abs(shaped[1:-1]) ** 2))
        noise_energy = 2 * float(np.sum(envelope[1:-1] ** 2))

        voice = voicing[frame]
        saw_gain = np.sin(voice * np.pi / 2) * np.sqrt(energy / saw_energy) if saw_energy > 1e-9 else 0.0
        noise_gain = np.cos(voice * np.pi / 2) * np.sqrt(energy / noise_energy) if noise_energy > 1e-9 else 0.0

        random = np.random.default_rng(seed + frame * 7919)
        angles = random.random(half + 1) * 2 * np.pi
        synth = np.zeros(half + 1, dtype=np.complex128)
        synth[1:half] = (
            saw_gain * shaped[1:half]
            + noise_gain * envelope[1:half] * np.exp(1j * angles[1:half])
        )
        wave = np.fft.irfft(synth, n=size)

        spectrum_sum[start : start + size] += wave * window
        weights[start : start + size] += window * window

    robot = np.divide(spectrum_sum, weights, out=np.zeros_like(spectrum_sum), where=weights > 1e-6)
    robot = robot[pad : pad + dry_samples.size]

    dry = np.cos(amount * np.pi / 2)
    wet = np.sin(amount * np.pi / 2)
    mixed = dry_samples * dry + robot * wet

    # The peaks bend over instead of squaring off.
    magnitude = np.abs(mixed)
    knee = 4767 / 32768
    ceiling = 28000 / 32768
    loud = magnitude > ceiling
    mixed[loud] = np.sign(mixed[loud]) * (ceiling + knee * np.tanh((magnitude[loud] - ceiling) / knee))
    return np.clip(mixed, -1.0, 1.0).astype(np.float32)
