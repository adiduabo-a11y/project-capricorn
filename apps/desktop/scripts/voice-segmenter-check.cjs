/**
 * Segmenter checks: synthetic frames, no microphone and no AudioContext.
 *
 *   npm run test:voice
 *
 * The compiled segmenter is passed in as argv[2]; `npm run test:voice`
 * produces it from `src/renderer/audio/segmenter.ts` first.
 */

const path = require('node:path')

const out = process.argv[2]
if (!out) {
  console.error('usage: node scripts/voice-segmenter-check.cjs <compiled-dir>')
  process.exit(2)
}
const { WakeSegmenter, MIN_MS, MAX_MS } = require(path.join(path.resolve(out), 'segmenter.js'))
const { Preroll } = require(path.join(path.resolve(out), 'preroll.js'))

const RATE = 16_000
const FRAME_MS = 20
const FRAME = (RATE * FRAME_MS) / 1000
const failures = []

function check(ok, label) {
  console.log(`  ${ok ? 'ok  ' : 'FAIL'} ${label}`)
  if (!ok) failures.push(label)
}

/** One frame of a sine at `level`; level 0 is silence. */
function frame(level) {
  const samples = new Float32Array(FRAME)
  if (level === 0) return samples
  for (let index = 0; index < FRAME; index += 1) samples[index] = level * Math.sin(index * 0.3)
  return samples
}

/** Feed `ms` of audio and return the last utterance produced, if any. */
function feed(segmenter, ms, level) {
  let utterance = null
  const frames = Math.round(ms / FRAME_MS)
  for (let index = 0; index < frames; index += 1) {
    utterance = segmenter.push(frame(level), RATE) ?? utterance
  }
  return utterance
}

const seconds = (samples) => samples.length / RATE

console.log('segmenter')

{
  const segmenter = new WakeSegmenter({ silenceMs: 700 })
  const utterance = feed(segmenter, 600, 0.3)
  check(utterance === null, 'an utterance in progress yields nothing yet')
  const done = feed(segmenter, 800, 0)
  check(done !== null, 'speech then silence ends an utterance')
  check(seconds(done) >= 0.5 && seconds(done) <= 1.6, `utterance is ${seconds(done).toFixed(2)} s`)
  check(done.some((value) => Math.abs(value) > 0.2), 'the utterance carries the speech')
}

{
  const segmenter = new WakeSegmenter({ silenceMs: 700 })
  const click = feed(segmenter, 60, 0.4)
  const done = feed(segmenter, 800, 0)
  check(click === null && done === null, `a click shorter than ${MIN_MS} ms is dropped`)
}

{
  const segmenter = new WakeSegmenter({ silenceMs: 700 })
  feed(segmenter, 500, 0.3)
  segmenter.reset()
  const done = feed(segmenter, 900, 0)
  check(done === null, 'reset forgets a partial utterance')
}

{
  const segmenter = new WakeSegmenter({ silenceMs: 700 })
  const done = feed(segmenter, 20_000, 0.3)
  check(done !== null, 'continuous speech is capped, not lost')
  check(seconds(done) <= MAX_MS / 1000 + 0.1, `capped at ${seconds(done).toFixed(2)} s`)
}

{
  const segmenter = new WakeSegmenter({ silenceMs: 700 })
  feed(segmenter, 400, 0)
  feed(segmenter, 500, 0.3)
  const done = feed(segmenter, 800, 0)
  check(done !== null, 'a quiet lead-in still yields the speech')
  check(seconds(done) >= 1.4, `pre-roll and the utterance are kept: ${seconds(done).toFixed(2)} s`)
}

{
  const segmenter = new WakeSegmenter({ silenceMs: 200 })
  feed(segmenter, 300, 0.3)
  const done = feed(segmenter, 250, 0)
  check(done !== null, 'a shorter silence gap ends it sooner')
}

{
  // Calibration raises the threshold in a noisy room; the gate has to obey it.
  const segmenter = new WakeSegmenter({ silenceMs: 700, thresholdRms: 0.4 })
  const opening = feed(segmenter, 800, 0.3)
  const done = feed(segmenter, 800, 0)
  check(opening === null && done === null, 'a calibrated threshold above this sound keeps it shut')
}

{
  // Calibration lowers the minimum when the name is short; it has to survive.
  const segmenter = new WakeSegmenter({ silenceMs: 700, thresholdRms: 0.05, minMs: 60 })
  feed(segmenter, 60, 0.3)
  const done = feed(segmenter, 800, 0)
  check(done !== null, 'a calibrated minimum keeps a short word')
}

console.log('preroll')

{
  const ring = new Preroll(200)
  for (let index = 0; index < 20; index += 1) ring.push(frame(0.1), RATE)
  const taken = ring.take(1000)
  const total = taken.reduce((sum, item) => sum + seconds(item.samples), 0) * 1000
  check(taken.length === 10, `the ring keeps only its window: ${taken.length} frames`)
  check(total <= 200, `and no more of it: ${Math.round(total)} ms`)
}

{
  const ring = new Preroll(200)
  for (let index = 0; index < 10; index += 1) ring.push(new Float32Array(FRAME).fill(index), RATE)
  const taken = ring.take(100)
  check(taken.length === 5, `a 100 ms ask gets five 20 ms frames: ${taken.length}`)
  check(taken[0].samples[0] === 5, `the window starts 100 ms back: frame ${taken[0].samples[0]}`)
  check(taken[4].samples[0] === 9, 'oldest first, newest last')
}

{
  const ring = new Preroll(200)
  check(ring.take(100).length === 0, 'a cold ring gives nothing, not silence')
  ring.push(frame(0.1), RATE)
  check(ring.take(100).length === 1, 'the first frame is available immediately')
  ring.clear()
  check(ring.take(100).length === 0, 'clear empties the ring')
}

{
  const ring = new Preroll(200)
  for (let index = 0; index < 5; index += 1) ring.push(frame(0.1), RATE)
  check(ring.take(1000).length === 5, 'a window wider than the buffer returns all of it')
}

console.log()
if (failures.length) {
  console.log(`FAILED: ${failures.length} check(s)`)
  process.exit(1)
}
console.log('all checks passed')
