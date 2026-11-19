/**
 * Capri's real sprite renderer.
 *
 * The source art is a set of horizontal strips. Each strip is cut into equal
 * cells at load time, then driven manually so state changes, blink overlays and
 * viseme mouths stay in one clock. No sprite-sheet metadata is guessed from a
 * filename beyond the counts declared in `pack.json`.
 */

import { Container, Rectangle, Sprite, Texture } from 'pixi.js'

import type { CapriFigure } from './placeholder'
import type { ClipDef, SpritePack } from './pack'

import idleSheet from '@packs/sprites/capri-neon/sprites/idle.png'
import idleBlinkSheet from '@packs/sprites/capri-neon/sprites/idle_blink.png'
import idleMouthSheet from '@packs/sprites/capri-neon/sprites/idle_mouth.png'
import glitchSheet from '@packs/sprites/capri-neon/sprites/glitch.png'
import sleepSheet from '@packs/sprites/capri-neon/sprites/sleep.png'
import walkSheet from '@packs/sprites/capri-neon/sprites/walk.png'
import dangleSheet from '@packs/sprites/capri-neon/sprites/dangle.png'
import grinSheet from '@packs/sprites/capri-neon/sprites/grin.png'
import danceSheet from '@packs/sprites/capri-neon/sprites/dance.png'
import wakeSheet from '@packs/sprites/capri-neon/sprites/wake.png'
import leaveSheet from '@packs/sprites/capri-neon/sprites/leave.png'
import perchSheet from '@packs/sprites/capri-neon/sprites/perch.png'
import perchTalkSheet from '@packs/sprites/capri-neon/sprites/perch_talk.png'
import perchBlinkSheet from '@packs/sprites/capri-neon/sprites/perch_blink.png'
import perchGlitchSheet from '@packs/sprites/capri-neon/sprites/perch_glitch.png'
import perchMusicSheet from '@packs/sprites/capri-neon/sprites/perch_music.png'
import perchArriveSheet from '@packs/sprites/capri-neon/sprites/perch_arrive.png'
import perchLeaveSheet from '@packs/sprites/capri-neon/sprites/perch_leave.png'

const SHEETS: Record<string, string> = {
  idle: idleSheet,
  idle_blink: idleBlinkSheet,
  idle_mouth: idleMouthSheet,
  glitch: glitchSheet,
  sleep: sleepSheet,
  walk: walkSheet,
  dangle: dangleSheet,
  grin: grinSheet,
  dance: danceSheet,
  wake: wakeSheet,
  leave: leaveSheet,
  perch: perchSheet,
  perch_talk: perchTalkSheet,
  perch_blink: perchBlinkSheet,
  perch_glitch: perchGlitchSheet,
  perch_music: perchMusicSheet,
  perch_arrive: perchArriveSheet,
  perch_leave: perchLeaveSheet,
}

interface Clip {
  frames: Texture[]
  fps: number
  loop: boolean
}

function cutStrip(sheet: Texture, count: number): Texture[] {
  if (!Number.isInteger(count) || count < 1) throw new Error('sprite clip count must be positive')
  if (sheet.width % count !== 0) {
    throw new Error(`sprite strip ${sheet.width}px wide cannot be cut into ${count} cells`)
  }
  const width = sheet.width / count
  return Array.from({ length: count }, (_, index) => {
    return new Texture({
      source: sheet.source,
      frame: new Rectangle(index * width, 0, width, sheet.height),
    })
  })
}

function loadTexture(url: string): Promise<Texture> {
  return new Promise((resolve, reject) => {
    const image = new Image()
    image.onload = () => resolve(Texture.from(image))
    image.onerror = () => reject(new Error(`failed to load sprite sheet ${url}`))
    image.src = url
  })
}

async function loadClips(defs: Record<string, ClipDef>): Promise<Record<string, Clip>> {
  const names = [...new Set(Object.values(defs).map((def) => def.sheet))]
  const textures = new Map<string, Texture>()

  await Promise.all(
    names.map(async (name) => {
      const url = SHEETS[name]
      if (!url) throw new Error(`sprite sheet "${name}" is not bundled`)
      textures.set(name, await loadTexture(url))
    }),
  )

  const clips: Record<string, Clip> = {}
  for (const [name, def] of Object.entries(defs)) {
    const sheet = textures.get(def.sheet)
    if (!sheet) throw new Error(`sprite sheet "${def.sheet}" was not loaded`)
    clips[name] = {
      frames: cutStrip(sheet, def.count),
      fps: def.fps,
      loop: def.loop ?? true,
    }
  }
  return clips
}

export async function createSpriteCapri(pack: SpritePack): Promise<CapriFigure> {
  if (!pack.clips) throw new Error(`sprite pack "${pack.name}" has no clips`)
  const clips = await loadClips(pack.clips)

  const view = new Container()
  const body = new Sprite(clips.idle?.frames[0])
  body.anchor.set(0.5, 1)
  view.addChild(body)

  const mouth = new Sprite()
  mouth.anchor.set(0.5, 1)
  mouth.visible = false
  view.addChild(mouth)

  const blink = new Sprite()
  blink.anchor.set(0.5, 1)
  blink.visible = false
  view.addChild(blink)

  let stateName = pack.states[pack.defaultState] ? pack.defaultState : 'idle'
  let def = pack.states[stateName] ?? {}
  let activeClip = resolveClip(def.clip)
  let frameAt = 0
  let frameIndex = -1
  // A held cell (the seated sleep) stops the clock instead of looping.
  let frozen = false
  // The clip's correction from the anchor, in canvas pixels. Zero for every
  // clip cut around her feet; the perch strips are cut around her hip.
  let shiftX = 0
  let shiftY = 0
  // Character size, relative to the pack's reference clips (the perch strips).
  // The sheets were drawn at different sizes, so every clip carries its own.
  let poseScale = 1
  let facingSign = 1
  let blinkClock = 0
  let blinkLeft = 0
  let blinkAt = 0
  let elapsed = 0
  let viseme = 'closed'
  let mouthFrames: Texture[] | null = null
  let mouthMap: Record<string, string> = {}

  function resolveClip(name: string | undefined): Clip {
    const picked = (name && clips[name]) || clips.idle
    if (!picked) throw new Error(`sprite pack "${pack.name}" needs an idle clip`)
    return picked
  }

  function showFrame(index: number): void {
    if (index === frameIndex) return
    frameIndex = index
    body.texture = activeClip.frames[index]
  }

  function refreshMouth(): void {
    if (!mouthFrames || stateName !== 'talking') {
      mouth.visible = false
      return
    }
    const picked = Number.parseInt(mouthMap[viseme] ?? mouthMap.closed ?? '0', 10)
    const frame = Number.isFinite(picked) ? Math.min(Math.max(picked, 0), mouthFrames.length - 1) : 0
    mouth.texture = mouthFrames[frame]
    mouth.visible = true
  }

  function setState(name: string): void {
    if (!pack.states[name]) return
    stateName = name
    def = pack.states[name]
    activeClip = resolveClip(def.clip)
    const clipDef = pack.clips?.[def.clip ?? '']
    shiftX = clipDef?.shift?.[0] ?? 0
    shiftY = clipDef?.shift?.[1] ?? 0
    poseScale = clipDef?.scale ?? 1
    applyScale()
    frozen = def.frame != null
    frameAt = 0
    frameIndex = -1
    blinkClock = 0
    blinkLeft = 0
    blink.visible = false

    const mouthClip = def.mouthClip ? clips[def.mouthClip] : undefined
    mouthFrames = mouthClip?.frames ?? null
    mouthMap = def.mouth ?? {}
    refreshMouth()

    body.alpha = name === 'sleeping' ? 0.88 : 1

    // Swap the first frame now instead of waiting for the next ticker step.
    // A command like Dance or Sleep should look immediate on the desktop.
    showFrame(frozen ? clampFrame(def.frame) : 0)
  }

  function clampFrame(index: number | undefined): number {
    const picked = Number.isFinite(index) ? Number(index) : 0
    return Math.min(Math.max(picked, 0), activeClip.frames.length - 1)
  }

  function setMouth(value: string): void {
    viseme = value
    refreshMouth()
  }

  function applyScale(): void {
    body.scale.set(poseScale * facingSign, poseScale)
    mouth.scale.set(poseScale * facingSign, poseScale)
    blink.scale.set(poseScale * facingSign, poseScale)
  }

  function setFacing(next: 'left' | 'right'): void {
    facingSign = next === 'left' ? -1 : 1
    applyScale()
  }

  function update(dtMs: number): void {
    const dt = Math.min(dtMs, 100) / 1000
    elapsed += dt

    if (!frozen) {
      frameAt += dt
      const step = 1 / Math.max(activeClip.fps, 1)
      while (frameAt >= step) {
        frameAt -= step
        let next = frameIndex + 1
        if (next >= activeClip.frames.length) {
          next = activeClip.loop ? 0 : activeClip.frames.length - 1
        }
        showFrame(next)
      }
      if (frameIndex < 0) showFrame(0)
    }

    // Standing clips get the artist's own blink strip laid over the loop.
    const every = def.blink ?? 0
    if (every > 0 && def.blinkClip && clips[def.blinkClip]) {
      blinkClock += dt
      if (blinkLeft <= 0 && blinkClock >= every) {
        blinkClock = 0
        blinkLeft = clips[def.blinkClip].frames.length / Math.max(clips[def.blinkClip].fps, 1)
        blinkAt = 0
      }
    }
    if (blinkLeft > 0 && def.blinkClip && clips[def.blinkClip]) {
      const clip = clips[def.blinkClip]
      blinkLeft = Math.max(0, blinkLeft - dt)
      blinkAt += dt
      const index = Math.min(Math.floor(blinkAt * clip.fps), clip.frames.length - 1)
      blink.texture = clip.frames[index]
      blink.visible = blinkLeft > 0
    } else {
      blink.visible = false
    }

    const motion = def.motion ?? 'bob'
    const lift = motion === 'sleep' ? 0.8 : 1.8
    body.x = shiftX
    blink.x = shiftX
    mouth.x = shiftX
    body.y = Math.sin(elapsed * (motion === 'sleep' ? 0.7 : 1.25)) * lift + shiftY
    if (blink.visible) blink.y = body.y
    mouth.y = body.y
    refreshMouth()
  }

  /**
   * The drawn art for the current pose, in stage pixels. The cell is mostly
   * transparent padding — a 240 px cell around a 161 px figure — so the pet's
   * clickable region is cut to the clip's trim box rather than the cell.
   */
  function inkRect(): { x: number; y: number; width: number; height: number } | null {
    const texture = body.texture
    if (!texture) return null
    const [trimX, trimY, trimW, trimH] =
      pack.clips?.[def.clip ?? '']?.trim ?? [0, 0, texture.width, texture.height]
    // Cell-local corners with the anchor at bottom centre, then the sprite's
    // own scale/flip, then the stage transform.
    const left = -texture.width / 2
    const top = -texture.height
    const xs = [left + trimX, left + trimX + trimW].map(
      (vx) => view.position.x + (body.x + vx * body.scale.x) * view.scale.x,
    )
    const ys = [top + trimY, top + trimY + trimH].map(
      (vy) => view.position.y + (body.y + vy * body.scale.y) * view.scale.y,
    )
    return {
      x: Math.min(xs[0], xs[1]),
      y: Math.min(ys[0], ys[1]),
      width: Math.abs(xs[1] - xs[0]),
      height: Math.abs(ys[1] - ys[0]),
    }
  }

  setState(stateName)
  update(0)

  return {
    view,
    setState,
    setMouth,
    setFacing,
    update,
    inkRect,
    get state() {
      return stateName
    },
  }
}

