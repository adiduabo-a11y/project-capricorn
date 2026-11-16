/**
 * The capture worklet has to survive the build as a standalone module.
 *
 * `AudioWorklet.addModule` fetches exactly one file, so an asset that imports a
 * shared chunk would fail at runtime with nothing useful in the console. This
 * checks the built output, not the source.
 *
 *   npm run test:worklet   (builds first)
 */

const fs = require('node:fs')
const path = require('node:path')

const dist = path.join(__dirname, '..', 'dist', 'assets')
const failures = []

function check(ok, label) {
  console.log(`  ${ok ? 'ok  ' : 'FAIL'} ${label}`)
  if (!ok) failures.push(label)
}

if (!fs.existsSync(dist)) {
  console.error('no build output: run npm run build first')
  process.exit(2)
}

const assets = fs.readdirSync(dist).filter((name) => /worklet.*\.js$/.test(name))
check(assets.length === 1, `one worklet asset is emitted: ${assets.join(', ') || 'none'}`)
if (assets.length !== 1) {
  console.log(`\nFAILED: ${failures.length} check(s)`)
  process.exit(1)
}

const file = assets[0]
const source = fs.readFileSync(path.join(dist, file), 'utf8')
check(
  /registerProcessor\(\s*["']capri-capture["']/.test(source),
  'the asset registers the capri-capture processor',
)
check(!/^\s*(import|export)\s/m.test(source), 'the asset is standalone, with no module imports')
check(source.length < 8_000, `the asset is small: ${source.length} bytes`)

const index = fs.readdirSync(dist).find((name) => /^index-.*\.js$/.test(name))
const bundle = index ? fs.readFileSync(path.join(dist, index), 'utf8') : ''
check(index !== undefined, `the main bundle is there: ${index ?? 'none'}`)
check(bundle.includes(file), `the bundle points at the asset: ${file}`)
check(
  /new URL\(\s*["'][^"']*worklet[^"']*["']\s*,\s*import\.meta\.url\s*\)/.test(bundle),
  'the worklet URL resolves against the document, not the working directory',
)

console.log()
if (failures.length) {
  console.log(`FAILED: ${failures.length} check(s)`)
  process.exit(1)
}
console.log('all checks passed')
