/**
 * The few hundred milliseconds before a press.
 *
 * A cold device takes longer to open than the first word takes to say, so a
 * segment would otherwise start mid-syllable. The capture graph keeps the last
 * few frames here, and a listener that asks for them gets them before the live
 * stream.
 *
 * Pure: frames in, frames out, no AudioContext and no clock of its own, so
 * `npm run test:voice` can exercise it without a microphone.
 */

export interface PrerollFrame {
  samples: Float32Array
  rate: number
}

const frameMs = (frame: PrerollFrame): number =>
  (frame.samples.length / Math.max(1, frame.rate)) * 1000

export class Preroll {
  private frames: PrerollFrame[] = []
  private kept = 0

  constructor(private readonly windowMs: number) {}

  /** Keep this frame for anyone who subscribes within the window. */
  push(samples: Float32Array, rate: number): void {
    const frame = { samples, rate }
    this.frames.push(frame)
    this.kept += frameMs(frame)
    while (this.frames.length > 1 && this.kept > this.windowMs) {
      const dropped = this.frames.shift()
      if (!dropped) break
      this.kept -= frameMs(dropped)
    }
  }

  /** The most recent frames, oldest first, adding up to at most `wantedMs`. */
  take(wantedMs: number): PrerollFrame[] {
    const taken: PrerollFrame[] = []
    let total = 0
    for (let index = this.frames.length - 1; index >= 0; index -= 1) {
      const frame = this.frames[index]
      if (total + frameMs(frame) > wantedMs) break
      taken.unshift(frame)
      total += frameMs(frame)
    }
    return taken
  }

  /** A new device is a new clock: the old frames do not belong to it. */
  clear(): void {
    this.frames = []
    this.kept = 0
  }
}
