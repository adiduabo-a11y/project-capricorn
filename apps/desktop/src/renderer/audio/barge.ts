/**
 * Barge-in monitor.
 *
 * While her speech is playing, watch the shared microphone with a short energy
 * gate. Echo cancellation keeps her own voice from triggering it; a sustained
 * signal (default 120 ms) means the user started talking. The callback stops
 * playback locally first, then tells the daemon, so the audible interruption
 * does not wait for an RPC round trip.
 */

import { micCapture, type FrameListener } from './capture'
import { rms } from './wav'

export interface BargeOptions {
  threshold: number
  holdMs: number
}

export class BargeMonitor {
  private unsubscribe: (() => void) | null = null
  private above = 0
  private fired = false
  private startedAt = 0

  constructor(
    private readonly options: BargeOptions,
    private readonly onBargeIn: () => void,
  ) {}

  start(): void {
    if (this.unsubscribe) return
    this.fired = false
    this.above = 0
    this.startedAt = performance.now()
    // Best effort: `subscribe` opens the graph and swallows a missing
    // microphone, and push-to-talk still works when the user asks for it.
    this.unsubscribe = micCapture.subscribe(this.onFrame)
  }

  /** One batch of frames: a sustained rise over the gate means they spoke. */
  private readonly onFrame: FrameListener = (samples, rate) => {
    if (this.fired) return
    // Let the echo canceller settle, but do not spend the whole interruption
    // budget calibrating. A loud first word must count immediately; the
    // configurable threshold plus echo cancellation is the noise gate.
    if (performance.now() - this.startedAt < 60) return
    const ms = (samples.length / Math.max(1, rate)) * 1000
    if (rms(samples) > this.options.threshold) this.above += ms
    else this.above = Math.max(0, this.above - ms * 2)
    if (this.above >= this.options.holdMs) {
      this.fired = true
      this.stop()
      this.onBargeIn()
    }
  }

  stop(): void {
    this.unsubscribe?.()
    this.unsubscribe = null
    this.above = 0
  }
}
