/**
 * Push-to-talk recording.
 *
 * The daemon does not open an audio device. The shared capture graph hands this
 * class its frames while the button is held; on release it joins them, trims
 * obvious silence, downsamples to 16 kHz and hands the data URL to
 * `voice.listen`. Nothing is persisted; the buffer dies after the RPC.
 */

import { micCapture, PREROLL_MS, type FrameListener } from './capture'
import { TARGET_RATE, resample, rms, wavBase64 } from './wav'

const MAX_MS = 60_000
const FRAME_MS = 20

export interface Recording {
  audioUrl: string
  durationMs: number
  hadVoice: boolean
}

/** Trim leading/trailing quiet frames, but keep a little padding. */
function trimSilence(samples: Float32Array, rate: number): Float32Array {
  const frame = Math.max(1, Math.round((rate * FRAME_MS) / 1000))
  const frames = Math.ceil(samples.length / frame)
  const voiced: number[] = []
  for (let index = 0; index < frames; index += 1) {
    const start = index * frame
    const end = Math.min(samples.length, start + frame)
    if (rms(samples.subarray(start, end)) >= 0.008) voiced.push(index)
  }
  if (voiced.length === 0) return samples.slice(0, 0)
  const first = Math.max(0, voiced[0] - 8) * frame
  const last = Math.min(frames, voiced[voiced.length - 1] + 9) * frame
  return samples.slice(first, Math.min(samples.length, last))
}

export class PushToTalk {
  private unsubscribe: (() => void) | null = null
  private chunks: Float32Array[] = []
  private rate = 48_000
  private timer: number | null = null

  get recording(): boolean {
    return this.unsubscribe !== null
  }

  async start(): Promise<void> {
    if (this.unsubscribe) return
    this.chunks = []
    // The key goes down before the device finishes opening: the frames from
    // just before the press are part of what was said.
    const unsubscribe = micCapture.subscribe(this.collect, PREROLL_MS)
    this.unsubscribe = unsubscribe
    this.timer = window.setTimeout(() => {
      try {
        this.stop()
      } catch {
        /* already stopped, or nothing worth sending */
      }
    }, MAX_MS)
    try {
      // The shared graph opens lazily; surfacing its failure here is what lets
      // the button report "permission denied" instead of recording nothing.
      await micCapture.start()
    } catch (err) {
      this.release()
      throw err
    }
  }

  private readonly collect: FrameListener = (samples, rate) => {
    this.chunks.push(samples)
    this.rate = rate
  }

  stop(): Recording {
    if (!this.unsubscribe) throw new Error('the microphone was not started')
    const rate = this.rate
    const chunks = this.chunks
    this.release()
    const joinedLength = chunks.reduce((total, chunk) => total + chunk.length, 0)
    const joined = new Float32Array(joinedLength)
    let offset = 0
    for (const chunk of chunks) {
      joined.set(chunk, offset)
      offset += chunk.length
    }
    const trimmed = trimSilence(joined, rate)
    const durationMs = Math.round((trimmed.length / rate) * 1000)
    if (durationMs < 180) throw new Error('recording too short — hold the mic and speak')
    const samples = resample(trimmed, rate, TARGET_RATE)
    return {
      audioUrl: wavBase64(samples, TARGET_RATE),
      durationMs: Math.round((samples.length / TARGET_RATE) * 1000),
      hadVoice: trimmed.length > 0,
    }
  }

  cancel(): void {
    this.release()
    this.chunks = []
  }

  /** Let go of the graph; the buffer belongs to the caller after `stop`. */
  private release(): void {
    if (this.timer !== null) {
      window.clearTimeout(this.timer)
      this.timer = null
    }
    this.unsubscribe?.()
    this.unsubscribe = null
  }
}
