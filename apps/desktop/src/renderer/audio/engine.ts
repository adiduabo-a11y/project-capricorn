/**
 * One AudioContext for Capri's speech.
 *
 * The speaker path is always live. The same speech bus can be tapped by the
 * mic mix, which sends Capri and the user's microphone into a virtual cable
 * without routing the microphone back into the speakers.
 */

export class AudioEngine {
  private ctx: AudioContext | null = null
  private speaker: GainNode | null = null
  private bus: GainNode | null = null

  context(): AudioContext {
    if (!this.ctx) {
      this.ctx = new AudioContext()
      this.speaker = this.ctx.createGain()
      this.speaker.gain.value = 0.9
      this.speaker.connect(this.ctx.destination)
      this.bus = this.ctx.createGain()
      this.bus.gain.value = 1
      this.bus.connect(this.speaker)
    }
    return this.ctx
  }

  /** Everything Capri says, before the speaker volume. */
  get speech(): GainNode {
    this.context()
    return this.bus as GainNode
  }

  async resume(): Promise<AudioContext> {
    const ctx = this.context()
    if (ctx.state === 'suspended') await ctx.resume().catch(() => undefined)
    return ctx
  }
}
