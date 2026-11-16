/**
 * Wake word capture.
 *
 * "Capri" is not a trained wake word — there is no model for it — so the
 * microphone stays open and the daemon's own speech recogniser decides whether
 * an utterance opened with her name. The renderer therefore only segments
 * speech; the decision lives in `segmenter.ts` and the daemon.
 *
 * Frames come from the shared capture graph, so the wake gate, barge-in and
 * push-to-talk use one device opening and one echo canceller instead of three.
 */

import { micCapture, type FrameListener } from './capture'
import { WakeSegmenter } from './segmenter'
import { TARGET_RATE, resample, wavBase64 } from './wav'

/** Everything that shapes the gate, as opposed to what it does with a hit. */
export type WakeSettings = Pick<WakeOptions, 'silenceMs' | 'thresholdRms' | 'minMs'>

export interface WakeOptions {
  /** Silence that ends an utterance, in milliseconds. */
  silenceMs: number
  /** RMS above which a frame counts as speech; calibrated. */
  thresholdRms?: number
  /** Shortest voiced sound worth transcribing; calibrated. */
  minMs?: number
  onUtterance: (audioUrl: string) => void
}

export class WakeMonitor {
  private readonly segmenter: WakeSegmenter
  private unsubscribe: (() => void) | null = null
  private paused = false

  constructor(private readonly options: WakeOptions) {
    this.segmenter = new WakeSegmenter(options)
  }

  /** True when this monitor already runs with these numbers. */
  matches(settings: WakeSettings): boolean {
    return (
      this.options.silenceMs === settings.silenceMs &&
      this.options.thresholdRms === settings.thresholdRms &&
      this.options.minMs === settings.minMs
    )
  }

  async start(): Promise<void> {
    if (this.unsubscribe) return
    this.paused = false
    this.unsubscribe = micCapture.subscribe(this.onFrame)
    try {
      await micCapture.start()
    } catch (err) {
      this.stop()
      throw err
    }
  }

  private readonly onFrame: FrameListener = (samples, rate) => {
    if (this.paused) return
    const utterance = this.segmenter.push(samples, rate)
    if (!utterance) return
    this.options.onUtterance(wavBase64(resample(utterance, rate, TARGET_RATE), TARGET_RATE))
  }

  /**
   * Stop taking utterances without closing the microphone: the graph is shared,
   * so leaving it open avoids a device reopen every time she starts talking.
   */
  setPaused(paused: boolean): void {
    this.paused = paused
    if (paused) this.segmenter.reset()
  }

  stop(): void {
    this.segmenter.reset()
    this.unsubscribe?.()
    this.unsubscribe = null
  }
}
