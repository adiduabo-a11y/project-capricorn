/**
 * Sprite pack loading.
 *
 * A pack is pure data: `pack.json` describes states, palette and anchor, plus
 * either a generated figure (provider: "generated") or horizontal sprite
 * strips (provider: "sprite"). Real frames can replace the placeholder without
 * touching any component code.
 */

import placeholder from '@packs/sprites/capri-placeholder/pack.json'
import neon from '@packs/sprites/capri-neon/pack.json'

export interface StateDef {
  motion?: 'bob' | 'still' | 'hover' | 'sleep'
  fps?: number
  loop?: boolean
  blink?: number
  fx?: string[]
  frames?: string[]
  mouth?: Record<string, string>
  clip?: string
  blinkClip?: string
  mouthClip?: string
  /** Hold this cell and stop the clock: a seated sleep must not blink. */
  frame?: number
  description?: string
}

export interface Palette {
  body: string
  bodyMid: string
  bodyDeep: string
  ink: string
  pink: string
  pinkDeep: string
  green: string
  greenDeep: string
  eye: string
  white: string
  danger: string
}

/** One horizontal strip, cut into equal-width cells at load time. */
export interface ClipDef {
  sheet: string
  count: number
  fps: number
  loop?: boolean
  /**
   * Where the art sits relative to the anchor, in canvas pixels. Perch strips
   * are cut around her hip rather than her feet, so they carry the correction
   * the source project applies; every other clip leaves it at zero.
   */
  shift?: [number, number]
  /**
   * Telexel scale for the strip. The sheets were drawn at different character
   * sizes; every clip carries the factor that puts it at the perch art's size,
   * which is the pack's reference. Absent means 1.
   */
  scale?: number
  /**
   * The art's bounding box inside the cell, as [x, y, width, height] in sheet
   * pixels. Union over the clip's frames; drives the pet's clickable region so
   * the window stops swallowing desktop clicks in the transparent corners.
   */
  trim?: [number, number, number, number]
}

export interface SpritePack {
  name: string
  displayName: string
  version: string
  provider: 'generated' | 'sprite'
  canvas: [number, number]
  anchor: string
  defaultState: string
  palette: Palette
  states: Record<string, StateDef>
  clips?: Record<string, ClipDef>
}

const KNOWN: Record<string, unknown> = {
  'capri-placeholder': placeholder,
  'capri-neon': neon,
}

export class PackError extends Error {}

export function parsePack(raw: unknown): SpritePack {
  const data = raw as Partial<SpritePack>
  if (!data || typeof data !== 'object') throw new PackError('pack must be an object')
  if (!data.name) throw new PackError('pack.name is required')
  if (!data.palette || !data.states) throw new PackError('pack needs palette and states')
  if (!Array.isArray(data.canvas) || data.canvas.length !== 2) {
    throw new PackError('pack.canvas must be [width, height]')
  }
  const provider = (data as { provider?: string }).provider
  if (provider === 'atlas') {
    throw new PackError(
      `pack "${data.name}" uses an atlas, but only horizontal strips are supported`,
    )
  }
  if (provider !== 'generated' && provider !== 'sprite') {
    throw new PackError(`pack "${data.name}" has an unknown provider`)
  }
  if (provider === 'sprite' && (!data.clips || Object.keys(data.clips).length === 0)) {
    throw new PackError(`sprite pack "${data.name}" needs clips`)
  }
  return {
    name: data.name,
    displayName: data.displayName ?? data.name,
    version: data.version ?? '0.0.0',
    provider,
    canvas: data.canvas as [number, number],
    anchor: data.anchor ?? 'feet-center',
    defaultState: data.defaultState ?? 'idle',
    palette: data.palette as Palette,
    states: data.states as Record<string, StateDef>,
    clips: data.clips,
  }
}

export function loadPack(name: string): SpritePack {
  const raw = KNOWN[name]
  if (!raw) {
    throw new PackError(
      `unknown sprite pack "${name}". Known packs: ${Object.keys(KNOWN).join(', ')}`,
    )
  }
  return parsePack(raw)
}
