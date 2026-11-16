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
