/**
 * Speech segmentation for the wake gate.
 *
 * Pure: frames in, one utterance out. No AudioContext, no `navigator`, no
 * window — the Web Audio plumbing lives in `wake.ts` — so this is exercised
 * directly by `npm run test:voice` with synthetic frames.
 */

import { rms } from './wav'

/** RMS above which a frame counts as speech. */
export const SPEECH_RMS = 0.012
/** Shortest voiced sound worth transcribing: filters clicks and coughs. */
export const MIN_MS = 300
/**
 * Below this much voiced audio the utterance may be her name and nothing else,
 * so a pause after it is not the end of the sentence.
 */
export const SHORT_MS = 1200
/** Hard cap, so a noisy room cannot grow one buffer forever. */
export const MAX_MS = 15_000
/** Audio kept before the first voiced frame, so the first syllable survives. */
export const PREROLL_MS = 240

export interface SegmenterOptions {
  /** Silence that ends an utterance, in milliseconds. */
  silenceMs: number
  /**
   * Extra silence allowed while too little has been said to be a sentence.
   * "Capri" is short, and the request comes after the pause.
   */
  graceMs?: number
  /** RMS above which a frame counts as speech. Calibration writes this. */
  thresholdRms?: number
  /** Shortest voiced sound worth transcribing. Calibration writes this. */
  minMs?: number
}

export class WakeSegmenter {
  private readonly silenceMs: number
  private readonly graceMs: number
  private readonly thresholdRms: number
  private readonly minMs: number
  private preroll: Float32Array[] = []
  private prerollMs = 0
  private voiced: Float32Array[] = []
  private voicedMs = 0
  private speechMs = 0
  private quietMs = 0
  private capturing = false

  constructor(options: SegmenterOptions) {
    this.silenceMs = options.silenceMs
    this.graceMs = Math.max(0, options.graceMs ?? 0)
    this.thresholdRms = options.thresholdRms ?? SPEECH_RMS
    this.minMs = options.minMs ?? MIN_MS
  }

  /** Feed one frame. Returns the utterance's samples when one just ended. */
  push(frame: Float32Array, rate: number): Float32Array | null {
    const frameMs = (frame.length / Math.max(1, rate)) * 1000
    if (!this.capturing) {
      this.pushPreroll(frame, frameMs)
      if (rms(frame) < this.thresholdRms) return null
      this.capturing = true
      this.voiced = this.preroll
      this.voicedMs = this.prerollMs
      // The frame that opened the gate is the one loud frame we know about.
      this.speechMs = frameMs
      this.preroll = []
      this.prerollMs = 0
      this.quietMs = 0
      return null
    }
    this.voiced.push(frame)
    this.voicedMs += frameMs
    if (rms(frame) >= this.thresholdRms) {
      this.speechMs += frameMs
      this.quietMs = 0
    } else {
      this.quietMs += frameMs
    }
    // A short opening may be her name on its own: give the request a moment to
    // arrive instead of ending the utterance and losing it.
    const limit = this.speechMs < SHORT_MS ? this.silenceMs + this.graceMs : this.silenceMs
    if (this.quietMs < limit && this.voicedMs < MAX_MS) return null
    return this.take()
  }

  /** Forget a partial utterance: the microphone is changing hands. */
  reset(): void {
    this.preroll = []
    this.prerollMs = 0
    this.voiced = []
    this.voicedMs = 0
    this.speechMs = 0
    this.quietMs = 0
    this.capturing = false
  }

  /** Frames arrive from one processor, so they share a duration. */
  private pushPreroll(frame: Float32Array, frameMs: number): void {
    this.preroll.push(frame)
    this.prerollMs += frameMs
    while (this.preroll.length > 1 && this.prerollMs > PREROLL_MS) {
      this.preroll.shift()
      this.prerollMs -= frameMs
    }
  }

  private take(): Float32Array | null {
    const chunks = this.voiced
    const speech = this.speechMs
    this.reset()
    // Voiced time, not total time: a click plus its silence is not speech.
    if (speech < this.minMs) return null
    const total = chunks.reduce((count, chunk) => count + chunk.length, 0)
    const joined = new Float32Array(total)
    let offset = 0
    for (const chunk of chunks) {
      joined.set(chunk, offset)
      offset += chunk.length
    }
    return joined
  }
}
