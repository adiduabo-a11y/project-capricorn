/**
 * Rebuilds the per-clip `trim` boxes in a sprite pack from the sheets' alpha.
 *
 * `trim` is the union of a clip's opaque pixels, as [x, y, width, height] in
 * sheet pixels. The renderer cuts the pet window's clickable region down to it
 * (`inkRect()` in src/renderer/avatar/sprite.ts), so a re-exported sheet with a
 * stale box silently leaves part of her unreachable: rebuild after any export.
 *
 *   node scripts/trim-sprites.mjs           rewrite the boxes in pack.json
 *   node scripts/trim-sprites.mjs --check   exit 1 when they drifted from the art
 *   node scripts/trim-sprites.mjs <pack>    another pack.json (paths to its
 *                                           sprites/ resolve beside it)
 *
 * No dependencies: the art is 8-bit, non-interlaced PNG, so the decoder only
 * needs IHDR/PLTE/tRNS/IDAT.
 */

import { readFileSync, writeFileSync } from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'
import { inflateSync } from 'node:zlib'

const here = path.dirname(fileURLToPath(import.meta.url))

export const DEFAULT_PACK = path.resolve(here, '../../../packs/sprites/capri-neon/pack.json')

/**
 * Pixels at or below this alpha are treated as empty. The exporter leaves a
 * faint halo around the art that should not stretch the clickable box.
 */
export const ALPHA_FLOOR = 16

const CHANNELS = { 0: 1, 2: 3, 3: 1, 4: 2, 6: 4 }

function unfilter(raw, width, height, channels) {
  const stride = width * channels
  const out = Buffer.alloc(stride * height)
  let pos = 0
  for (let y = 0; y < height; y++) {
    const filter = raw[pos++]
    const row = y * stride
    const prev = row - stride
    for (let x = 0; x < stride; x++) {
      const value = raw[pos++]
      const left = x >= channels ? out[row + x - channels] : 0
      const up = y > 0 ? out[prev + x] : 0
      const upLeft = x >= channels && y > 0 ? out[prev + x - channels] : 0
      let pixel
      switch (filter) {
        case 0:
          pixel = value
          break
        case 1:
          pixel = value + left
          break
        case 2:
          pixel = value + up
          break
        case 3:
          pixel = value + ((left + up) >> 1)
          break
        case 4: {
          const estimate = left + up - upLeft
          const toLeft = Math.abs(estimate - left)
          const toUp = Math.abs(estimate - up)
          const toUpLeft = Math.abs(estimate - upLeft)
          pixel = value + (toLeft <= toUp && toLeft <= toUpLeft ? left : toUp <= toUpLeft ? up : upLeft)
          break
        }
        default:
          throw new Error(`row ${y}: unknown PNG filter ${filter}`)
      }
      out[row + x] = pixel & 0xff
    }
  }
  return out
}

function decodePng(file) {
  const buf = readFileSync(file)
  const signature = Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a])
  if (!buf.subarray(0, 8).equals(signature)) throw new Error(`${file}: not a PNG`)

  let header = null
  let transparency = null
  const idat = []
  for (let pos = 8; pos + 8 <= buf.length; ) {
    const length = buf.readUInt32BE(pos)
    const type = buf.toString('ascii', pos + 4, pos + 8)
    const data = buf.subarray(pos + 8, pos + 8 + length)
    if (type === 'IHDR') {
      header = {
        width: data.readUInt32BE(0),
        height: data.readUInt32BE(4),
        bitDepth: data[8],
        colorType: data[9],
        interlace: data[12],
      }
    } else if (type === 'tRNS') {
      transparency = data
    } else if (type === 'IDAT') {
      idat.push(data)
    } else if (type === 'IEND') {
      break
    }
    pos += 12 + length
  }
  if (!header) throw new Error(`${file}: no IHDR`)

  const { width, height, bitDepth, colorType, interlace } = header
  const channels = CHANNELS[colorType]
  if (bitDepth !== 8 || interlace !== 0 || !channels) {
    throw new Error(
      `${file}: unsupported PNG (bit depth ${bitDepth}, colour type ${colorType}, interlace ${interlace})`,
    )
  }
  return {
    width,
    height,
    colorType,
    channels,
    transparency,
    pixels: unfilter(inflateSync(Buffer.concat(idat)), width, height, channels),
  }
}

/** One alpha byte per pixel, or null when the sheet has no transparency. */
function alphaChannel(png) {
  const { width, height, colorType, channels, transparency, pixels } = png
  const count = width * height
  if (colorType === 6 || colorType === 4) {
    const out = new Uint8Array(count)
    const offset = channels - 1
    for (let i = 0; i < count; i++) out[i] = pixels[i * channels + offset]
    return out
  }
  if (colorType === 3 && transparency) {
    const lut = new Uint8Array(256).fill(255)
    for (let i = 0; i < transparency.length && i < 256; i++) lut[i] = transparency[i]
    const out = new Uint8Array(count)
    for (let i = 0; i < count; i++) out[i] = lut[pixels[i]]
    return out
  }
  return null
}

/** Union box of the opaque pixels across a sheet's frames, in cell pixels. */
export function sheetTrim(file, count) {
  const png = decodePng(file)
  if (!Number.isInteger(count) || count < 1 || png.width % count !== 0) {
    throw new Error(`${file}: width ${png.width} does not split into ${count} frames`)
  }
  const cell = png.width / count
  const alpha = alphaChannel(png)
  let x0 = Infinity
  let y0 = Infinity
  let x1 = -1
  let y1 = -1
  for (let y = 0; y < png.height; y++) {
    const row = y * png.width
    for (let frame = 0; frame < count; frame++) {
      const left = frame * cell
      for (let x = 0; x < cell; x++) {
        if (alpha && alpha[row + left + x] <= ALPHA_FLOOR) continue
        if (x < x0) x0 = x
        if (x > x1) x1 = x
        if (y < y0) y0 = y
        if (y > y1) y1 = y
      }
    }
  }
  if (x1 < 0) return null
  return [x0, y0, x1 - x0 + 1, y1 - y0 + 1]
}

/** Fresh boxes for every sheet the pack's clips use, keyed by sheet name. */
export function computeTrims(packPath = DEFAULT_PACK) {
  const pack = readPack(packPath)
  const sheets = new Map()
  for (const [name, clip] of Object.entries(pack.clips)) {
    const known = sheets.get(clip.sheet)
    if (known !== undefined && known !== clip.count) {
      throw new Error(
        `${name}: sheet ${clip.sheet} is cut into ${clip.count} frames here but ${known} elsewhere`,
      )
    }
    sheets.set(clip.sheet, clip.count)
  }

  const sprites = path.join(path.dirname(packPath), 'sprites')
  const trims = {}
  for (const [sheet, count] of sheets) {
    const box = sheetTrim(path.join(sprites, `${sheet}.png`), count)
    if (!box) throw new Error(`${sheet}: every frame is empty`)
    trims[sheet] = box
  }
  return trims
}

export function readPack(packPath = DEFAULT_PACK) {
  return JSON.parse(readFileSync(packPath, 'utf8'))
}

/** Current art boxes plus every clip whose stored box disagrees with them. */
export function inspectTrims(packPath = DEFAULT_PACK) {
  const pack = readPack(packPath)
  const trims = computeTrims(packPath)
  const drift = []
  for (const [name, clip] of Object.entries(pack.clips)) {
    const art = trims[clip.sheet]
    const stored = clip.trim ?? null
    if (!stored || stored.length !== 4 || stored.some((value, i) => value !== art[i])) {
      drift.push({ clip: name, sheet: clip.sheet, stored, art })
    }
  }
  return { pack, trims, drift, sheets: Object.keys(trims).length, clips: Object.keys(pack.clips).length }
}

function clipBlock(text, name) {
  const key = `"${name}": {`
  const start = text.indexOf(key)
  if (start < 0) return null
  const open = start + key.length
  let depth = 0
  for (let i = open; i < text.length; i++) {
    if (text[i] === '{') depth += 1
    else if (text[i] === '}') {
      if (depth === 0) return { open, close: i }
      depth -= 1
    }
  }
  return null
}

const literal = (box) => `[${box.join(', ')}]`

/**
 * Rewrites the boxes in place, touching nothing else in the file: the pack is
 * hand-formatted one clip per line and a full re-serialise would bury the diff.
 */
export function writeTrims(packPath, trims) {
  const pack = readPack(packPath)
  let text = readFileSync(packPath, 'utf8')
  let changed = 0
  for (const [name, clip] of Object.entries(pack.clips)) {
    const want = trims[clip.sheet]
    if (!want) continue
    const block = clipBlock(text, name)
    if (!block) throw new Error(`${packPath}: cannot find clip ${name}`)
    const body = text.slice(block.open, block.close)
    const next = /"trim"\s*:\s*\[[^\]]*\]/.test(body)
      ? body.replace(/"trim"\s*:\s*\[[^\]]*\]/, `"trim": ${literal(want)}`)
      : ` "trim": ${literal(want)},${body.replace(/^\s+/, ' ')}`
    if (next === body) continue
    text = text.slice(0, block.open) + next + text.slice(block.close)
    changed += 1
  }
  if (changed > 0) writeFileSync(packPath, text)
  return changed
}

function main(argv) {
  const check = argv.includes('--check')
  const packPath = argv.find((arg) => !arg.startsWith('--')) ?? DEFAULT_PACK
  const report = inspectTrims(packPath)

  if (report.drift.length === 0) {
    console.log(`[trim] ${report.clips} clips over ${report.sheets} sheets match the art`)
    return 0
  }
  if (check) {
    for (const item of report.drift) {
      console.error(
        `[trim] ${item.clip} (${item.sheet}): stored ${JSON.stringify(item.stored)} but art is ${literal(item.art)}`,
      )
    }
    console.error('[trim] stale trims; rebuild with: npm --workspace @capri/desktop run trim')
    return 1
  }

  const changed = writeTrims(packPath, report.trims)
  console.log(`[trim] rewrote ${changed} clip(s) in ${packPath}`)
  return 0
}

const invokedDirectly =
  process.argv[1] !== undefined && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)
if (invokedDirectly) process.exitCode = main(process.argv.slice(2))
