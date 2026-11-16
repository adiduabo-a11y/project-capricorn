/**
 * Recording for wake calibration.
 *
 * The daemon measures; this only captures. Frames come from the shared graph,
 * which runs with the same audio processing the wake gate uses, so the numbers
 * mean the same thing when the gate is armed later — auto gain and noise
 * suppression are what decide how loud "loud" is.
 */

import { micCapture, type FrameListener } from './capture'
import { TARGET_RATE, resample, rms, wavBase64 } from './wav'

export interface CalibrationRecorderOptions {
  /** How long to record, in milliseconds. */
  ms: number
  /** Called for every batch with its RMS, for the level meter. */
  onLevel?: (level: number) => void
  /** Progress through the recording, 0..1. */
  onProgress?: (fraction: number) => void
}

export class CalibrationRecorder {
  private chunks: Float32Array[] = []
  private rate = TARGET_RATE
  private recordedMs = 0
  private resolve: ((url: string | null) => void) | null = null
  private unsubscribe: (() => void) | null = null
  private closed = false

  constructor(private readonly options: CalibrationRecorderOptions) {}

  /** Record for the configured duration. Resolves null when cancelled. */
  async start(): Promise<string | null> {
    const done = new Promise<string | null>((resolve) => {
      this.resolve = resolve
    })
    this.unsubscribe = micCapture.subscribe(this.collect)
    try {
      await micCapture.start()
    } catch (err) {
      this.finish(false)
      throw err
    }
    return done
  }

  private readonly collect: FrameListener = (samples, rate) => {
    this.chunks.push(samples)
    this.rate = rate
    this.recordedMs += (samples.length / Math.max(1, rate)) * 1000
    this.options.onLevel?.(rms(samples))
    this.options.onProgress?.(Math.min(1, this.recordedMs / Math.max(1, this.options.ms)))
    if (this.recordedMs >= this.options.ms) this.finish(true)
  }

  /** Stop now and keep what has been recorded. */
  stop(): void {
    this.finish(true)
  }

  /** Stop now and throw the recording away. */
  cancel(): void {
    this.finish(false)
  }

  private finish(deliver: boolean): void {
    if (this.closed) return
    const resolve = this.resolve
    const url = deliver ? this.toUrl() : null
    this.close()
    resolve?.(url)
  }

  private toUrl(): string | null {
    if (this.chunks.length === 0) return null
    const total = this.chunks.reduce((sum, chunk) => sum + chunk.length, 0)
    const joined = new Float32Array(total)
    let offset = 0
    for (const chunk of this.chunks) {
      joined.set(chunk, offset)
      offset += chunk.length
    }
    return wavBase64(resample(joined, this.rate, TARGET_RATE), TARGET_RATE)
  }

  private close(): void {
    this.closed = true
    this.unsubscribe?.()
    this.unsubscribe = null
    this.chunks = []
  }
}
