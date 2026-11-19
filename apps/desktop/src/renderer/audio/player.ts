/**
 * WebAudio playback for speech segments.
 *
 * The daemon ships a WAV data URL plus viseme timings per segment; the renderer
 * schedules the buffer and fires the mouth frames on those timings. It never
 * analyses audio — that analysis belongs to whichever process synthesized it.
 *
 * Segments are scheduled back to back against one clock, so a queue of them
 * plays gaplessly instead of restarting per sentence.
 *
 * Synthesis and playback run at different speeds. The daemon emits a segment
 * as soon as it is synthesized and then `speech.stop{done}`, which can be a
 * whole sentence ahead of what the speakers have finished. `pending` is what
 * playback actually owes, so the `done` stop waits for it instead of tearing
 * down the last sentence mid-word.
 */

import type { SpeechSegment } from '../bridge'
import type { AudioEngine } from './engine'

function bytesFromDataUrl(url: string): ArrayBuffer {
  const comma = url.indexOf(',')
  const payload = comma >= 0 ? url.slice(comma + 1) : url
  const binary = atob(payload)
  const bytes = new Uint8Array(binary.length)
  for (let index = 0; index < binary.length; index += 1) {
    bytes[index] = binary.charCodeAt(index)
  }
  return bytes.buffer
}

export class SpeechPlayer {
  private nextStart = 0
  private timers = new Set<number>()
  private sources = new Set<AudioBufferSourceNode>()
  /** Segments still decoding or playing. Zero is the only honest "idle". */
  private pending = 0
  /** Bumped by stop(): a decode that lands afterwards must not schedule itself
   *  into the next utterance as a stray fragment. */
  private epoch = 0
  /** Serializes decode+schedule so segment N+1 cannot overtake segment N. */
  private chain: Promise<void> = Promise.resolve()
  private readonly onMouth: (viseme: string | null) => void
  private readonly onIdle: () => void

  constructor(
    private readonly engine: AudioEngine,
    onMouth: (viseme: string | null) => void,
    onIdle: () => void = () => undefined,
  ) {
    this.onMouth = onMouth
    this.onIdle = onIdle
  }

  enqueue(segment: SpeechSegment): Promise<void> {
    const epoch = this.epoch
    this.pending += 1
    const run = this.chain.then(() => this.enqueueOrdered(segment, epoch))
    this.chain = run.catch(() => undefined)
    return run
  }

  private async enqueueOrdered(segment: SpeechSegment, epoch: number): Promise<void> {
    if (epoch !== this.epoch) return
    let scheduled = false
    try {
      const ctx = await this.engine.resume()
      if (epoch !== this.epoch) return

      let buffer: AudioBuffer
      try {
        buffer = await ctx.decodeAudioData(bytesFromDataUrl(segment.audioUrl))
      } catch (err) {
        console.error('[capri] speech decode failed', err)
        this.onMouth(null)
        return
      }
      // Stopped while this was decoding: the utterance is over, so this
      // segment belongs to nobody.
      if (epoch !== this.epoch) return

      const start = Math.max(ctx.currentTime + 0.02, this.nextStart)
      this.nextStart = start + buffer.duration

      const source = ctx.createBufferSource()
      source.buffer = buffer
      source.connect(this.engine.speech)
      this.sources.add(source)
      source.onended = () => {
        this.sources.delete(source)
        try {
          source.disconnect()
        } catch {
          /* already disconnected */
        }
        this.release(epoch)
      }
      source.start(start)

      const now = ctx.currentTime
      for (const frame of segment.visemes) {
        const delay = Math.max(0, (start + frame.t / 1000 - now) * 1000)
        const timer = window.setTimeout(() => {
          this.timers.delete(timer)
          this.onMouth(frame.v)
        }, delay)
        this.timers.add(timer)
      }

      const settle = Math.max(0, (start + buffer.duration - now) * 1000)
      const endTimer = window.setTimeout(() => {
        this.timers.delete(endTimer)
        // The last segment closes the mouth; earlier ones just stop moving it.
        this.onMouth(segment.last ? null : 'closed')
      }, settle)
      this.timers.add(endTimer)
      scheduled = true
    } finally {
      if (!scheduled) this.release(epoch)
    }
  }

  /** One segment is done decoding or playing. `stop()` may have moved on. */
  private release(epoch: number): void {
    if (epoch !== this.epoch) return
    this.pending = Math.max(0, this.pending - 1)
    if (this.pending === 0) this.onIdle()
  }

  stop(): void {
    this.epoch += 1
    for (const timer of this.timers) window.clearTimeout(timer)
    this.timers.clear()
    for (const source of this.sources) {
      try {
        source.stop()
      } catch {
        /* already ended */
      }
      try {
        source.disconnect()
      } catch {
        /* already disconnected */
      }
    }
    this.sources.clear()
    this.pending = 0
    this.nextStart = 0
    this.onMouth(null)
  }
}
