/**
 * Procedural placeholder figure for Capri.
 *
 * Deliberately data-driven: every colour comes from the pack palette and every
 * motion from `pack.states[name].motion`, so swapping in a real sprite atlas
 * later means replacing this module, not the component.
 *
 * Drawn in pack canvas space (260x380) with the origin pivoted to feet-centre.
 *
 * Two rules learned the hard way:
 *   1. Translucency is `Container.alpha` on a dedicated layer, never per-fill
 *      alpha. Pixi's Graphics fill alpha is unreliable across builds.
 *   2. Nothing may extend past her silhouette. A full-bleed overlay renders as
 *      a visible rectangle floating on the desktop.
 */

import { Container, Graphics } from 'pixi.js'

import type { SpritePack } from './pack'

const hex = (value: string): number => Number.parseInt(value.replace('#', ''), 16)

const VISEME_OPEN: Record<string, number> = {
  closed: 0,
  m: 0.15,
  u: 0.3,
  o: 0.7,
  e: 0.75,
  a: 1,
  i: 0.85,
}

const GLOW_ALPHA = 0.13
const RIM_ALPHA = 0.55
const SCAN_IDLE = 0.08
const SCAN_WORKING = 0.15
const WIRE_ALPHA = 0.4

export interface CapriFigure {
  /** Root node; position it, the figure handles its own bob. */
  view: Container
  setState(name: string): void
  setMouth(viseme: string): void
  setFacing(facing: 'left' | 'right'): void
  update(dtMs: number): void
  /**
   * The drawn art for the current pose, in the stage's own pixels (the canvas
   * element's coordinate space), or null while the art is unknown. The window's
   * touchable region follows this instead of a fixed box.
   */
  inkRect?(): { x: number; y: number; width: number; height: number } | null
  readonly state: string
}

export function createCapri(pack: SpritePack): CapriFigure {
  const pal = {
    body: hex(pack.palette.body),
    bodyMid: hex(pack.palette.bodyMid),
    bodyDeep: hex(pack.palette.bodyDeep),
    ink: hex(pack.palette.ink),
    pink: hex(pack.palette.pink),
    pinkDeep: hex(pack.palette.pinkDeep),
    green: hex(pack.palette.green),
    greenDeep: hex(pack.palette.greenDeep),
    eye: hex(pack.palette.eye),
    white: hex(pack.palette.white),
    danger: hex(pack.palette.danger),
  }

  const view = new Container()
  const figure = new Container()
  view.addChild(figure)

  // ---- ground glow -------------------------------------------------------
  const glow = new Graphics()
  glow.ellipse(130, 372, 62, 9).fill(pal.green)
  glow.ellipse(130, 371, 26, 4).fill(pal.pink)
  glow.alpha = GLOW_ALPHA
  figure.addChild(glow)

  // ---- hair mass (behind head) -------------------------------------------
  const hairBack = new Graphics()
  hairBack
    .poly([
      76, 158, 71, 118, 82, 84, 98, 62, 120, 50, 146, 52, 167, 66, 178, 90, 183, 120,
      178, 158, 164, 128, 156, 162, 144, 120, 132, 164, 120, 122, 108, 162, 98, 126,
      88, 160,
    ])
    .fill(pal.pinkDeep)
  figure.addChild(hairBack)

  // ---- body --------------------------------------------------------------
  const body = new Graphics()
  // legs — a wide gap plus a dark inner seam, so they never fuse into a column
  body.poly([100, 298, 122, 298, 119, 366, 102, 366]).fill(pal.bodyMid)
  body.poly([138, 298, 160, 298, 158, 366, 141, 366]).fill(pal.bodyMid)
  body.rect(122, 308, 2, 58).fill(pal.bodyDeep)
  body.rect(136, 308, 2, 58).fill(pal.bodyDeep)
  // boots
  body.poly([102, 360, 119, 360, 119, 374, 99, 374]).fill(pal.bodyDeep)
  body.poly([141, 360, 158, 360, 161, 374, 141, 374]).fill(pal.bodyDeep)
  // torso
  body.poly([95, 198, 165, 198, 159, 302, 105, 302]).fill(pal.body)
  // pelvis
  body.roundRect(105, 288, 50, 22, 8).fill(pal.body)
  // arms — offset outside the torso line so they read as limbs
  body.poly([93, 206, 108, 201, 101, 292, 87, 288]).fill(pal.bodyMid)
  body.poly([152, 201, 167, 206, 173, 288, 159, 292]).fill(pal.bodyMid)
  // neck
  body.roundRect(121, 162, 18, 26, 6).fill(pal.bodyDeep)
  // decals
  body.poly([103, 226, 157, 220, 157, 223, 103, 229]).fill(pal.pink)
  body.poly([103, 250, 157, 244, 157, 246, 103, 252]).fill(pal.pinkDeep)
  body.poly([109, 272, 153, 269, 153, 271, 109, 274]).fill(pal.greenDeep)
  figure.addChild(body)

  // ---- rim light (own layer: needs a stable alpha) -----------------------
  const rim = new Graphics()
  // left silhouette, pink
  rim.poly([95, 198, 98, 198, 108, 302, 105, 302]).fill(pal.pinkDeep)
  rim.poly([93, 208, 96, 206, 90, 288, 87, 288]).fill(pal.pinkDeep)
  rim.poly([100, 298, 103, 298, 102, 366, 99, 366]).fill(pal.pinkDeep)
  // right silhouette, green
  rim.poly([162, 198, 165, 198, 159, 302, 156, 302]).fill(pal.greenDeep)
  rim.poly([164, 206, 167, 208, 170, 288, 167, 288]).fill(pal.greenDeep)
  rim.poly([157, 298, 160, 298, 159, 366, 156, 366]).fill(pal.greenDeep)
  rim.alpha = RIM_ALPHA
  figure.addChild(rim)

  // ---- head --------------------------------------------------------------
  const head = new Graphics()
  head.ellipse(130, 134, 35, 40).fill(pal.body)
  head.ellipse(130, 150, 26, 20).fill(pal.body)
  figure.addChild(head)

  // ---- hair front: cap, spiked fringe, crown spikes ----------------------
  const hairFront = new Graphics()
  hairFront
    .poly([
      80, 150, 76, 110, 88, 78, 108, 60, 132, 52, 156, 60, 170, 80, 178, 110, 174,
      146, 164, 124, 158, 106, 148, 124, 140, 102, 130, 122, 120, 102, 110, 126, 100,
      106, 92, 128, 84, 112,
    ])
    .fill(pal.pink)
  // crown spikes — the silhouette cue that says "not a helmet"
  hairFront.poly([103, 63, 111, 33, 121, 57]).fill(pal.pink)
  hairFront.poly([125, 55, 136, 28, 146, 56]).fill(pal.pink)
  hairFront.poly([150, 58, 163, 40, 167, 66]).fill(pal.pink)
  hairFront.poly([86, 82, 91, 58, 101, 75]).fill(pal.pink)
  hairFront.poly([172, 86, 182, 66, 181, 94]).fill(pal.pink)
  figure.addChild(hairFront)

  // ---- side locks + green-tipped strand ----------------------------------
  const locks = new Graphics()
  locks
    .poly([82, 148, 87, 151, 80, 208, 76, 205])
    .fill(pal.pink)
    .poly([77, 203, 81, 206, 78, 230, 74, 228])
    .fill(pal.greenDeep)
    .poly([170, 132, 176, 156, 184, 198, 191, 226, 186, 228, 175, 200, 166, 158])
    .fill(pal.pink)
    .poly([187, 224, 191, 224, 196, 248, 190, 250])
    .fill(pal.green)
  figure.addChild(locks)

  // ---- eyes --------------------------------------------------------------
  const eyeGroup = new Container()
  const eyes = new Graphics()
  eyes.roundRect(-34, -15, 68, 30, 3).fill(pal.ink).stroke({ color: pal.green, width: 1.4 })
  for (const dx of [-18, 18]) {
    eyes.ellipse(dx, 0, 11, 7.5).fill(pal.ink)
    eyes.circle(dx, 0, 5.6).fill(pal.eye)
    eyes.circle(dx, 0, 2.3).fill(pal.white)
    eyes.ellipse(dx, 0, 11, 7.5).stroke({ color: pal.greenDeep, width: 0.9 })
  }
  eyeGroup.addChild(eyes)
  eyeGroup.position.set(130, 134)
  figure.addChild(eyeGroup)

  // ---- mouth -------------------------------------------------------------
  const mouth = new Graphics()
  mouth.roundRect(-7, -2, 14, 4, 2).fill(pal.ink)
  mouth.roundRect(-6, -1, 12, 2, 1).fill(pal.greenDeep)
  mouth.position.set(130, 160)
  figure.addChild(mouth)

  // ---- construct core ----------------------------------------------------
  const core = new Graphics()
  core.circle(0, 0, 3.2).fill(pal.green)
  core.circle(0, 0, 6.6).stroke({ color: pal.greenDeep, width: 1 })
  core.position.set(130, 240)
  figure.addChild(core)

  // ---- overlays ----------------------------------------------------------
  const wire = new Graphics()
  wire.poly([95, 198, 165, 198, 159, 302, 105, 302]).stroke({ color: pal.green, width: 1 })
  wire.ellipse(130, 134, 35, 40).stroke({ color: pal.green, width: 1 })
  wire.alpha = 0
  figure.addChild(wire)

  // Torso-only scanlines: stays inside the silhouette, so no visible box.
  const scan = new Graphics()
  for (let y = 204; y < 300; y += 3) scan.rect(102, y, 56, 1).fill(pal.white)
  scan.alpha = SCAN_IDLE
  figure.addChild(scan)

  figure.pivot.set(130, 372)

  // ---- state -------------------------------------------------------------
  let stateName = pack.states[pack.defaultState] ? pack.defaultState : 'idle'
  let def = pack.states[stateName] ?? {}
  let elapsed = 0
  let blinkClock = 0
  let blinkLeft = 0
  let glitchClock = 0
  let viseme: number | null = null

  const hasFx = (name: string) => (def.fx ?? []).includes(name)
  const isWorking = () => (def.motion ?? 'bob') === 'hover'

  function setState(name: string): void {
    if (!pack.states[name]) return
    stateName = name
    def = pack.states[name]

    // reset everything a previous state may have changed
    body.tint = 0xffffff
    head.tint = 0xffffff
    hairFront.tint = 0xffffff
    locks.tint = 0xffffff
    eyeGroup.position.x = 130
    eyeGroup.scale.set(1)
    figure.alpha = 1
    rim.alpha = RIM_ALPHA
    wire.alpha = hasFx('wireframe') ? WIRE_ALPHA : 0
    scan.alpha = isWorking() ? SCAN_WORKING : SCAN_IDLE
    glow.alpha = GLOW_ALPHA

    if (hasFx('redshift')) {
      body.tint = pal.danger
      head.tint = pal.danger
      hairFront.tint = pal.danger
      locks.tint = pal.danger
    }
    if (hasFx('dim') || stateName === 'sleeping') {
      figure.alpha = 0.55
      eyeGroup.scale.y = 0.06
      glow.alpha = 0.06
    }
  }

  function setMouth(value: string): void {
    viseme = VISEME_OPEN[value] ?? null
  }

  function update(dtMs: number): void {
    const dt = Math.min(dtMs, 100) / 1000
    elapsed += dt

    const motion = def.motion ?? 'bob'
    const bobAmp = motion === 'bob' ? 2.2 : motion === 'hover' ? 5 : motion === 'sleep' ? 1.2 : 0
    const bobSpeed = motion === 'sleep' ? 0.45 : 1.7
    figure.y = Math.sin(elapsed * bobSpeed) * bobAmp
    hairFront.rotation = Math.sin(elapsed * 0.9) * 0.014
    locks.rotation = Math.sin(elapsed * 1.25 + 1) * 0.03
    core.scale.set(1 + Math.sin(elapsed * 2.2) * 0.12)
    glow.alpha = GLOW_ALPHA + Math.sin(elapsed * 1.5) * 0.02

    // blink
    const every = def.blink ?? 4
    if (every > 0) {
      blinkClock += dt
      if (blinkClock >= every) {
        blinkClock = 0
        blinkLeft = 0.12
      }
    }
    if (blinkLeft > 0) {
      blinkLeft = Math.max(0, blinkLeft - dt)
      eyeGroup.scale.y = 0.08 + 0.92 * (1 - blinkLeft / 0.12)
    } else {
      eyeGroup.scale.y = every > 0 ? 1 : 0.06
    }

    // eye-box instability while thinking / erroring
    if (hasFx('glitch')) {
      glitchClock += dt
      if (glitchClock > 0.18) {
        glitchClock = 0
        eyeGroup.position.x = 130 + (Math.random() * 5 - 2.5)
      }
    }

    // mouth
    if (stateName === 'talking') {
      const open = viseme ?? 0.35 + Math.abs(Math.sin(elapsed * 9.5)) * 0.65
      mouth.scale.y = 0.4 + open * 2.4
      mouth.alpha = 1
    } else {
      mouth.scale.y = 1
      mouth.alpha = 0.7
    }

    // scanline density signals work
    if (motion === 'hover') scan.alpha = SCAN_WORKING + Math.sin(elapsed * 3) * 0.03
  }

  setState(stateName)

  return {
    view,
    setState,
    setMouth,
    setFacing(next) {
      figure.scale.x = next === 'left' ? -1 : 1
    },
    update,
    get state() {
      return stateName
    },
  }
}
