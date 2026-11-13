/**
 * Sprite pack loading.
 *
 * A pack is pure data: `pack.json` describes states, palette and anchor, and
 * either ships an atlas (provider: "atlas") or is drawn procedurally
 * (provider: "generated"). Real frames can replace the placeholder without
 * touching any component code.
 */

import placeholder from '@packs/sprites/capri-placeholder/pack.json'

export interface StateDef {
  motion?: 'bob' | 'still' | 'hover' | 'sleep'
  fps?: number
  loop?: boolean
  blink?: number
  fx?: string[]
  frames?: string[]
  mouth?: Record<string, string>
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

export interface SpritePack {
  name: string
  displayName: string
  version: string
  provider: 'generated' | 'atlas'
  canvas: [number, number]
  anchor: string
  defaultState: string
  palette: Palette
  states: Record<string, StateDef>
}

const KNOWN: Record<string, unknown> = {
  'capri-placeholder': placeholder,
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
  if (data.provider === 'atlas') {
    throw new PackError(
      `pack "${data.name}" uses an atlas, but atlas loading lands in phase 4`,
    )
  }
  return {
    name: data.name,
    displayName: data.displayName ?? data.name,
    version: data.version ?? '0.0.0',
    provider: 'generated',
    canvas: data.canvas as [number, number],
    anchor: data.anchor ?? 'feet-center',
    defaultState: data.defaultState ?? 'idle',
    palette: data.palette as Palette,
    states: data.states as Record<string, StateDef>,
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
