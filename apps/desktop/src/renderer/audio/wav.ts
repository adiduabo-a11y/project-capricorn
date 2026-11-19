/**
 * PCM helpers, free of Web Audio.
 *
 * Everything here is arithmetic over Float32 samples, so the segmentation and
 * capture code can be exercised without a microphone, an AudioContext or a
 * window (`npm run test:voice`).
 */

export const TARGET_RATE = 16_000

export function resample(samples: Float32Array, from: number, to: number): Float32Array {
  if (from === to || samples.length === 0) return samples
  const length = Math.max(1, Math.round((samples.length * to) / from))
  const out = new Float32Array(length)
  const ratio = from / to
  if (ratio > 1) {
    // Box anti-aliasing prefilter when downsampling so high-frequency energy
    // does not alias into the speech band sent to Whisper.
    const half = 0.5 * ratio
    for (let index = 0; index < length; index += 1) {
      const center = index * ratio
      const start = Math.max(0, Math.ceil(center - half))
      const end = Math.min(samples.length - 1, Math.floor(center + half))
      if (end < start) {
        const left = Math.min(samples.length - 1, Math.floor(center))
        const right = Math.min(samples.length - 1, left + 1)
        const frac = center - left
        out[index] = samples[left] * (1 - frac) + samples[right] * frac
        continue
      }
      let sum = 0
      for (let i = start; i <= end; i += 1) sum += samples[i]
      out[index] = sum / (end - start + 1)
    }
    return out
  }
  for (let index = 0; index < length; index += 1) {
    const position = index * ratio
    const left = Math.floor(position)
    const right = Math.min(samples.length - 1, left + 1)
    const fraction = position - left
    out[index] = samples[left] * (1 - fraction) + samples[right] * fraction
  }
  return out
}

export function wavBase64(samples: Float32Array, sampleRate: number): string {
  const buffer = new ArrayBuffer(44 + samples.length * 2)
  const view = new DataView(buffer)
  const write = (offset: number, text: string) => {
    for (let index = 0; index < text.length; index += 1) {
      view.setUint8(offset + index, text.charCodeAt(index))
    }
  }
  write(0, 'RIFF')
  view.setUint32(4, 36 + samples.length * 2, true)
  write(8, 'WAVE')
  write(12, 'fmt ')
  view.setUint32(16, 16, true)
  view.setUint16(20, 1, true)
  view.setUint16(22, 1, true)
  view.setUint32(24, sampleRate, true)
  view.setUint32(28, sampleRate * 2, true)
  view.setUint16(32, 2, true)
  view.setUint16(34, 16, true)
  write(36, 'data')
  view.setUint32(40, samples.length * 2, true)
  let offset = 44
  for (const value of samples) {
    const clamped = Math.max(-1, Math.min(1, value))
    view.setInt16(offset, clamped < 0 ? clamped * 0x8000 : clamped * 0x7fff, true)
    offset += 2
  }
  const bytes = new Uint8Array(buffer)
  let binary = ''
  for (let index = 0; index < bytes.length; index += 0x8000) {
    binary += String.fromCharCode(...bytes.subarray(index, index + 0x8000))
  }
  return `data:audio/wav;base64,${btoa(binary)}`
}

export function rms(samples: Float32Array): number {
  if (samples.length === 0) return 0
  let sum = 0
  for (const sample of samples) sum += sample * sample
  return Math.sqrt(sum / samples.length)
}
