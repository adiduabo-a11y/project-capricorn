import { useCallback, useEffect, useRef, useState } from 'react'

import { CapriStage } from './avatar/CapriStage'
import { capri, type AgentState } from './bridge'
import { FORM_HANDOFF_MS, FormArrival } from './FormArrival'
import { RibbonTrip } from './RibbonTrip'
import './pet.css'

const PET_W = 150
const PET_H = 225
// The seated art hangs below her feet line (scaled to match the stage).
// The canvas grows by this much to draw it.
const PERCH_ROOM = 135
// The pose gate: the room above the taskbar she needs for a seat to read as one.
const PERCH_GATE = 142
// Travel speed is per trip, not a constant: a fixed 64 px/s walk is the least
// alive thing a character can do.
const SPEED = 64
const WALK_MIN = 44
const WALK_MAX = 124
const GRAVITY = 2200
// The pet window is already the display work area, so this puts her feet just
// above the taskbar instead of letting the sprite hang below the window edge.
const FLOOR_OFFSET = 0
// A perch is any height above the floor: the corners up by the top of the
// screen, or anywhere in between. She keeps whichever height she lands at.
function perchY(baseY: number): number {
  return baseY * (0.04 + Math.random() * 0.34)
}
const POS_KEY = 'capri.pet.x'
const POS_Y_KEY = 'capri.pet.y'
const WANDER_KEY = 'capri.pet.wander'
const ANCHOR_KEY = 'capri.pet.anchor'
const TELEPORT_CHANCE = 0.3

// The seated clips are the pack's reference size, so they still fill the actor
// box: the hit region there is the box itself.
const HIT = { left: 22, top: 3, width: 105, height: 220 }
// Standing clips carry a clip scale to match the perch art, so she is drawn
// above and past the sides of the box. `onInk` reports the art's real bounds
// every frame and that is what the window follows; these two boxes are only the
// fallback for the frames before the stage has drawn her.
const SPRITE_RISE = 120
const HIT_STANDING = { left: -52, top: -SPRITE_RISE, width: 255, height: 220 + SPRITE_RISE }
// Canvas room for that art, in CSS px: CapriStage fits the pack canvas to the
// 150x225 box, so these margins add drawing space, not magnification.
const STAGE_RISE = 150
const STAGE_SIDE = 60

type Mode = 'idle' | 'walk' | 'drag' | 'fall'
type Reaction = 'arriving' | 'leaving' | 'clicked' | 'dancing' | 'waking' | 'annoyed' | null

const REACTION_MS: Record<Exclude<Reaction, null>, number> = {
  arriving: 1050,
  // The leave sheet is empty from frame 7 of 12 (~470 ms); the extra hold was
  // padding that read as her standing still after the glitch had finished.
  leaving: 540,
  clicked: 2050,
  dancing: 3200,
  waking: 1050,
  annoyed: 620,
}

// Thinking reads as brief signal loss, not a permanent shimmer: a short burst
// of the glitch clip, then idle, then another burst a second or two later.
const GLITCH_BURST_MS = 120
const GLITCH_GAP_MIN_MS = 1000
const GLITCH_GAP_MAX_MS = 2000
// A poke is a click that lands and releases on her without becoming a drag.
const POKE_WINDOW_MS = 6000
const POKE_LIMIT = 3
const POKE_GRACE_MS = 20_000
const POKE_HOP_MS = 420

function prefersReducedMotion(): boolean {
  try {
    return window.matchMedia('(prefers-reduced-motion: reduce)').matches
  } catch {
    return false
  }
}

function clamp(value: number, low: number, high: number): number {
  return Math.min(high, Math.max(low, value))
}

function moodFor(state: AgentState): string {
  switch (state) {
    case 'thinking':
      return 'thinking'
    case 'acting':
      return 'working'
    case 'speaking':
      return 'talking'
    case 'error':
      return 'error'
    case 'sleeping':
      return 'sleeping'
    default:
      return 'idle'
  }
}

/**
 * The same mood, sitting down. Standing art on a ledge is what made a perch look
 * like the floor; every ambient state has a seated counterpart in the pack.
 */
function perchedState(state: string): string {
  switch (state) {
    case 'thinking':
      return 'perched_thinking'
    case 'working':
      return 'perched_working'
    case 'talking':
      return 'perched_talking'
    case 'sleeping':
      return 'perched_sleeping'
    case 'listening':
      return 'perched_listening'
    case 'dancing':
      return 'perched_dancing'
    case 'arriving':
      return 'perched_arriving'
    case 'leaving':
      return 'perched_leaving'
    case 'annoyed':
    case 'error':
      return 'perched_thinking' // her glitch, seated
    default:
      return 'perched'
  }
}

/**
 * The desktop pet is its own transparent, screen-sized window. She walks a
 * floor line, glitch-teleports across it, can be picked up and tossed, and
 * turns back into chrome for a click. Only the sprite rectangle catches the
 * mouse; everything else remains click-through.
 */
export function Pet() {
  const actorRef = useRef<HTMLDivElement | null>(null)
  const [placed, setPlaced] = useState(false)
  const [mode, setMode] = useState<Mode>('idle')
  const [reaction, setReaction] = useState<Reaction>('arriving')
  const [facing, setFacing] = useState<'left' | 'right'>('right')
  const [agent, setAgent] = useState('idle')
  const [mouth, setMouth] = useState<string | null>(null)
  // A seat, not an altitude: only the pose gate flips this, so it can be used in
  // render without costing a state update on every frame.
  const [perched, setPerched] = useState(false)
  // The microphone is hot in the panel, which is a different process.
  const [listening, setListening] = useState(false)
  const [online, setOnline] = useState(true)
  const [wander, setWander] = useState(true)
  const [anchored, setAnchored] = useState(() => {
    try {
      return localStorage.getItem(ANCHOR_KEY) === 'true'
    } catch {
      return false
    }
  })
  const [forcedSleep, setForcedSleep] = useState(false)
  const [glitchBurst, setGlitchBurst] = useState(false)
  // One live ribbon trip: where she left, where she is going, and the top of
  // her body, so the strands can leave and land where she actually is.
  const [ribbon, setRibbon] = useState<{
    key: number
    fromX: number
    toX: number
    fromY: number
    toY: number
  } | null>(null)
  // One live landing: the drawings she forms from, and whether her own body is
  // still held back behind them.
  const [form, setForm] = useState<{
    key: number
    facing: 'left' | 'right'
  } | null>(null)
  const [veiled, setVeiled] = useState(false)

  const modeRef = useRef<Mode>('idle')
  const reactionRef = useRef<Reaction>('arriving')
  const wanderRef = useRef(true)
  const anchoredRef = useRef(anchored)
  const forcedSleepRef = useRef(false)
  const agentRef = useRef('idle')
  const perchedRef = useRef(false)
  const onlineRef = useRef(true)
  const mouthRef = useRef<string | null>(null)
  const interactiveRef = useRef(false)
  const pointer = useRef({ x: 0, y: 0, seen: false })
  const pos = useRef({ x: 0, y: 0, baseY: 0 })
  const lastBoundsAt = useRef(0)
  const targetX = useRef(0)
  const pendingTeleport = useRef<{ x: number; y: number } | null>(null)
  const idleAt = useRef(0)
  const speed = useRef(SPEED)
  // Teleports arrive in streaks: after one, another is likelier. Reset by a
  // completed walk, so it escalates and then settles instead of running away.
  const streak = useRef(0)
  const nextDither = useRef(0)
  const velocity = useRef({ x: 0, y: 0 })
  const pokes = useRef<number[]>([])
  const annoyedUntil = useRef(0)
  const annoyTimer = useRef<number | null>(null)
  const veilTimer = useRef<number | null>(null)
  const drag = useRef<{
    pointerId: number
    offsetX: number
    offsetY: number
    startX: number
    startY: number
    lastX: number
    lastY: number
    lastAt: number
    vx: number
    vy: number
    moved: boolean
  } | null>(null)

  const setModeSafe = useCallback((next: Mode) => {
    modeRef.current = next
    setMode(next)
  }, [])

  const clearRibbon = useCallback(() => setRibbon(null), [])
  const clearForm = useCallback(() => setForm(null), [])

  const setFacingSafe = useCallback((next: 'left' | 'right') => {
    setFacing((current) => (current === next ? current : next))
  }, [])

  // The chat bubble is a separate window anchored over the sprite. Report the
  // sprite box at animation speed so it can follow her walk, drag and toss.
  const reportBounds = useCallback((force = false) => {
    const now = performance.now()
    if (!force && now - lastBoundsAt.current < 33) return
    lastBoundsAt.current = now
    // Report what is drawn, not the actor box: the bubble hangs above her head,
    // and standing she reaches a long way above the box.
    const rise = perchedRef.current ? 0 : SPRITE_RISE
    capri.reportPetBounds({
      x: pos.current.x,
      y: pos.current.y - rise,
      width: PET_W,
      height: PET_H + rise,
    })
  }, [])

  const applyPosition = useCallback(() => {
    const actor = actorRef.current
    if (!actor) return
    actor.style.transform = `translate3d(${pos.current.x}px, ${pos.current.y}px, 0)`
    reportBounds()
  }, [reportBounds])

  // The drawn art for the pose on screen, reported by the stage. It is what the
  // window is allowed to intercept: a fixed box around her left a wide strip of
  // desktop dead behind her hair.
  const spriteRect = useRef<{ left: number; top: number; width: number; height: number } | null>(
    null,
  )
  const onInk = useCallback(
    (rect: { left: number; top: number; width: number; height: number } | null) => {
      spriteRect.current = rect
      const actor = actorRef.current
      if (!actor) return
      actor.dataset.hit = rect
        ? `${Math.round(rect.left)},${Math.round(rect.top)},${Math.round(rect.width)},${Math.round(rect.height)}`
        : ''
    },
    [],
  )

  const hitTest = useCallback((x: number, y: number): boolean => {
    const p = pos.current
    const art = spriteRect.current
    if (art) {
      // A generous pad of grace so the outline is easy to grab.
      const pad = 8
      return (
        x >= p.x + art.left - pad &&
        x <= p.x + art.left + art.width + pad &&
        y >= p.y + art.top - pad &&
        y <= p.y + art.top + art.height + pad
      )
    }
    // Before the stage has reported: fall back to the actor box.
    const box = perchedRef.current ? HIT : HIT_STANDING
    const inX = x >= p.x + box.left && x <= p.x + box.left + box.width
    if (inX && y >= p.y + box.top && y <= p.y + box.top + box.height) return true
    // A seated pet's legs hang below the actor box. They are still her, so the
    // window has to stay reachable where they are, or the pointer passes
    // through her knees to whatever is on the desktop behind.
    return (
      perchedRef.current &&
      inX &&
      y > p.y + PET_H &&
      y <= p.y + PET_H + PERCH_ROOM
    )
  }, [])

  const setInteractive = useCallback((next: boolean) => {
    if (interactiveRef.current === next) return
    interactiveRef.current = next
    capri.petPointer(next)
  }, [])

  const savePosition = useCallback(() => {
    try {
      localStorage.setItem(POS_KEY, String(Math.round(pos.current.x)))
      localStorage.setItem(POS_Y_KEY, String(Math.round(pos.current.y)))
    } catch {
      /* persistence is optional */
    }
  }, [])

  // Her reference teleport is two clips: glitch out where she is, move while
  // the screen is empty, then glitch back in at the new spot.
  const beginTeleport = useCallback(
    (targetX: number, targetY?: number) => {
      if (modeRef.current === 'drag' || modeRef.current === 'fall') return
      if (reactionRef.current === 'leaving') return
      const p = pos.current
      const maxX = Math.max(0, window.innerWidth - PET_W)
      const destination = clamp(targetX, 0, maxX)
      // Without a target height she keeps the one she is already at, so a
      // sideways trip does not drop her off a perch.
      const landing = clamp(targetY ?? p.y, 0, p.baseY)
      pendingTeleport.current = { x: destination, y: landing }
      reactionRef.current = 'leaving'
      // Her ribbons show the landing before she gets there, so they are only
      // worth drawing when she is actually going somewhere.
      const moved = Math.abs(destination - p.x) >= 12 || Math.abs(landing - p.y) >= 12
      if (!prefersReducedMotion() && moved) {
        setRibbon({
          key: performance.now(),
          fromX: p.x,
          toX: destination,
          fromY: p.y,
          toY: landing,
        })
      } else {
        setRibbon(null)
      }
      setForm(null)
      setVeiled(false)
      setForcedSleep(false)
      setModeSafe('idle')
      setFacingSafe(destination < p.x ? 'left' : 'right')
      setReaction('leaving')
      idleAt.current = performance.now() + 2600
    },
    [setFacingSafe, setModeSafe],
  )

  // Three pokes in a row are a nuisance, not a pat: one annoyed flash, then a
  // short hop away. Body only - nothing is spoken and no daemon call is made.
  const poke = useCallback(
    (now: number): boolean => {
      if (agentRef.current === 'thinking' || agentRef.current === 'working') return false
      if (agentRef.current === 'talking' || mouthRef.current) return false
      if (modeRef.current === 'drag' || modeRef.current === 'fall') return false
      if (!onlineRef.current || forcedSleepRef.current) return false
      if (now < annoyedUntil.current) return false
      pokes.current = pokes.current.filter((stamp) => now - stamp <= POKE_WINDOW_MS)
      pokes.current.push(now)
      if (pokes.current.length < POKE_LIMIT) return false
      pokes.current = []
      annoyedUntil.current = now + POKE_GRACE_MS
      setModeSafe('idle')
      setReaction('annoyed')
      idleAt.current = now + 1200
      annoyTimer.current = window.setTimeout(() => {
        annoyTimer.current = null
        if (modeRef.current === 'drag' || modeRef.current === 'fall') return
        if (anchoredRef.current) {
          setReaction(null)
          return
        }
        const maxX = Math.max(0, window.innerWidth - PET_W)
        if (maxX < 24) return
        const direction = pos.current.x < maxX / 2 ? 1 : -1
        beginTeleport(
          clamp(pos.current.x + direction * (48 + Math.random() * 40), 0, maxX),
        )
      }, POKE_HOP_MS)
      return true
    },
    [beginTeleport, setModeSafe],
  )

  // A jump reads as impatience, not travel: no target, just the floor.
  const hop = useCallback(() => {
    if (modeRef.current === 'drag' || modeRef.current === 'fall') return
    if (anchoredRef.current || forcedSleepRef.current) return
    velocity.current = { x: (Math.random() - 0.5) * 280, y: -540 - Math.random() * 240 }
    setReaction(null)
    setModeSafe('fall')
  }, [setModeSafe])

  // What the user said, and what she is doing on the screen, both land here.
  // The cue says why to move; the movement itself is hers.
  const reactToCue = useCallback(
    (cue: string) => {
      if (cue === 'dance') {
        if (forcedSleepRef.current || !onlineRef.current) return
        setModeSafe('idle')
        setReaction('dancing')
        return
      }
      // Pinned means pinned: a passing request does not undo "stay".
      if (anchoredRef.current) return
      const maxX = Math.max(0, window.innerWidth - PET_W)
      if (maxX < 24) return
      const p = pos.current
      const pointerX = pointer.current.seen ? pointer.current.x : window.innerWidth / 2
      const third = maxX / 3
      switch (cue) {
        case 'come': {
          const near = clamp(pointerX - PET_W / 2, 0, maxX)
          const nearY = pointer.current.seen
            ? clamp(pointer.current.y - PET_H * 0.56, 0, p.baseY)
            : p.y
          if (Math.abs(near - p.x) < 70 && Math.abs(nearY - p.y) < 70) {
            setReaction('clicked')
            idleAt.current = performance.now() + 1600
          } else {
            beginTeleport(near, nearY)
          }
          break
        }
        case 'away':
          beginTeleport(p.x < maxX / 2 ? maxX : 0)
          break
        case 'left':
          beginTeleport(clamp(p.x - maxX * (0.2 + Math.random() * 0.45), 0, maxX))
          break
        case 'right':
          beginTeleport(clamp(p.x + maxX * (0.2 + Math.random() * 0.45), 0, maxX))
          break
        case 'blink':
          beginTeleport(
            Math.random() * maxX,
            Math.random() < 0.5 ? perchY(p.baseY) : p.baseY,
          )
          break
        case 'hop':
          hop()
          break
        case 'watch':
          // Step out of the middle third: that is where the window she is
          // reading is, and she should not be standing in front of it.
          if (p.x > third && p.x < third * 2) {
            beginTeleport(p.x < maxX / 2 ? third * 0.3 : maxX - third * 0.3)
          }
          break
        case 'settle':
          if (modeRef.current === 'walk') setModeSafe('idle')
          idleAt.current = performance.now() + 6000
          break
      }
    },
    [beginTeleport, hop, setModeSafe],
  )

  useEffect(() => capri.onPetCue(reactToCue), [reactToCue])

  // 8A: bursts, not a permanent shimmer. `thinking` is the base state; during
  // the gap she stands idle so the glitch reads as signal loss rather than art.
  const thinking =
    agent === 'thinking' && mode === 'idle' && !reaction && !mouth && online && !forcedSleep

  useEffect(() => {
    if (!thinking || prefersReducedMotion()) {
      setGlitchBurst(false)
      return
    }
    let burstTimer = 0
    let gapTimer = 0
    const cycle = () => {
      setGlitchBurst(true)
      burstTimer = window.setTimeout(() => {
        setGlitchBurst(false)
        gapTimer = window.setTimeout(
          cycle,
          GLITCH_GAP_MIN_MS + Math.random() * (GLITCH_GAP_MAX_MS - GLITCH_GAP_MIN_MS),
        )
      }, GLITCH_BURST_MS)
    }
    gapTimer = window.setTimeout(cycle, 400)
    return () => {
      window.clearTimeout(burstTimer)
      window.clearTimeout(gapTimer)
    }
  }, [thinking])

  // Movement, gravity, and the idle -> walk -> idle loop.
  useEffect(() => {
    let raf = 0
    let last = performance.now()
    let loaded = false

    const layout = () => {
      const p = pos.current
      const maxX = Math.max(0, window.innerWidth - PET_W)
      p.baseY = window.innerHeight - PET_H + FLOOR_OFFSET

      if (!loaded) {
        const raw = localStorage.getItem(POS_KEY)
        const saved = raw == null ? Number.NaN : Number(raw)
        p.x = Number.isFinite(saved) ? saved : maxX * 0.65
        const rawY = localStorage.getItem(POS_Y_KEY)
        const savedY = rawY == null ? Number.NaN : Number(rawY)
        // A saved height is only worth restoring if she was up on a perch (or
        // pinned there); a save from the floor just puts her back on the floor.
        p.y =
          Number.isFinite(savedY) && (anchoredRef.current || savedY < p.baseY - 1)
            ? clamp(savedY, 0, p.baseY)
            : p.baseY
        idleAt.current = performance.now() + 2400
        loaded = true
      }
      p.x = clamp(p.x, 0, maxX)
      targetX.current = clamp(targetX.current, 0, maxX)
      if (modeRef.current !== 'drag' && modeRef.current !== 'fall') {
        // A perch keeps its height when the work area changes; otherwise she
        // stands on the floor the resize just moved.
        const perching = p.y < p.baseY - 1
        p.y = perching ? clamp(p.y, 0, p.baseY) : p.baseY
      }
      applyPosition()
      reportBounds(true)
      setPlaced(true)
    }

    const startMove = () => {
      if (anchoredRef.current) return
      if (!wanderRef.current || forcedSleepRef.current || mouthRef.current) return
      if (agentRef.current !== 'idle' || reactionRef.current) return
      const p = pos.current
      const maxX = Math.max(0, window.innerWidth - PET_W)
      if (maxX < 24) return
      const perched = p.y < p.baseY - 1
      let candidate = Math.random() * maxX
      if (Math.abs(candidate - p.x) < 110) {
        const leap = 220 + Math.random() * 320
        candidate = p.x < maxX / 2 ? Math.min(maxX, p.x + leap) : Math.max(0, p.x - leap)
      }
      if (Math.abs(candidate - p.x) < 32) {
        candidate = p.x < maxX / 2 ? maxX : 0
      }

      // She does not take turns politely. A teleport makes another likelier,
      // and a jump is always on the table.
      const roll = Math.random()
      if (perched) {
        // There is nowhere to walk to in the air: she either drops or travels
        // to another height.
        if (roll < 0.4) hop()
        else beginTeleport(candidate, roll < 0.7 ? p.baseY : perchY(p.baseY))
        return
      }
      if (roll < TELEPORT_CHANCE + Math.min(0.2, streak.current * 0.1)) {
        // Teleports are not floor-bound: she likes the corners by the top of
        // the screen as much as the ones by the taskbar.
        const high = Math.random() < 0.45
        const corner = Math.random() < 0.6
        beginTeleport(
          corner ? (Math.random() < 0.5 ? 0 : maxX) : candidate,
          high ? perchY(p.baseY) : p.baseY,
        )
        return
      }
      if (roll > 0.9) {
        hop()
        return
      }
      streak.current = 0
      speed.current = WALK_MIN + Math.random() * (WALK_MAX - WALK_MIN)
      nextDither.current = 0
      targetX.current = candidate
      setFacingSafe(candidate < p.x ? 'left' : 'right')
      setModeSafe('walk')
    }

    const step = (now: number) => {
      const p = pos.current
      const dt = Math.min((now - last) / 1000, 0.05)
      last = now
      const maxX = Math.max(0, window.innerWidth - PET_W)

      // The seated art hangs 176 CSS px below her hip, so a height that leaves
      // less than that under it would clip her legs at the window edge. This is
      // the pose gate; the movement's own `p.y < p.baseY - 1` is "in the air",
      // which is still true for a hop that is too low to sit at.
      const seated = p.y <= p.baseY - PERCH_GATE
      if (seated !== perchedRef.current) {
        perchedRef.current = seated
        setPerched(seated)
      }

      if (modeRef.current === 'walk') {
        if (
          anchoredRef.current ||
          !wanderRef.current ||
          forcedSleepRef.current ||
          mouthRef.current ||
          reactionRef.current ||
          agentRef.current !== 'idle'
        ) {
          setModeSafe('idle')
          idleAt.current = now + 1800
        } else {
          // Mid-walk she changes her mind: a new heading, or a beat of standing
          // still as if something caught her eye.
          let paused = false
          if (now >= nextDither.current) {
            nextDither.current = now + 900 + Math.random() * 2400
            if (Math.random() < 0.24) {
              targetX.current = clamp(p.x + (Math.random() - 0.5) * 560, 0, maxX)
              speed.current = WALK_MIN + Math.random() * (WALK_MAX - WALK_MIN)
            }
            if (Math.random() < 0.12) {
              setModeSafe('idle')
              idleAt.current = now + 240 + Math.random() * 800
              paused = true
            }
          }
          if (!paused) {
            const dx = targetX.current - p.x
            if (Math.abs(dx) < 1.5) {
              p.x = clamp(targetX.current, 0, maxX)
              setModeSafe('idle')
              streak.current = 0
              // Usually a real rest; sometimes she is off again at once.
              idleAt.current =
                now +
                (Math.random() < 0.2
                  ? 320 + Math.random() * 900
                  : 1400 + Math.random() * 5200)
              savePosition()
            } else {
              p.x = clamp(
                p.x + Math.sign(dx) * Math.min(Math.abs(dx), speed.current * dt),
                0,
                maxX,
              )
              setFacingSafe(dx < 0 ? 'left' : 'right')
            }
          }
          applyPosition()
        }
      } else if (modeRef.current === 'fall') {
        if (anchoredRef.current) {
          p.y = clamp(p.y, 0, p.baseY)
          velocity.current = { x: 0, y: 0 }
          setModeSafe('idle')
          savePosition()
          applyPosition()
        } else {
          p.x += velocity.current.x * dt
          p.y += velocity.current.y * dt
          velocity.current.y += GRAVITY * dt
          velocity.current.x *= 0.992
          if (p.x <= 0 || p.x >= maxX) {
            p.x = clamp(p.x, 0, maxX)
            velocity.current.x *= -0.35
          }
          if (p.y >= p.baseY) {
            p.y = p.baseY
            velocity.current = { x: 0, y: 0 }
            setModeSafe('idle')
            idleAt.current = now + 2200 + Math.random() * 3000
            savePosition()
          }
          applyPosition()
        }
      } else if (modeRef.current === 'idle') {
        // She keeps whatever height she landed at. Only a hop, a toss or a
        // walk on the floor brings her back down.
        if (
          wanderRef.current &&
          !forcedSleepRef.current &&
          !mouthRef.current &&
          !reactionRef.current &&
          agentRef.current === 'idle' &&
          now >= idleAt.current
        ) {
          startMove()
        }
      }

      // If she walks out from under a stationary cursor, drop click-through
      // back on; otherwise the old sprite rectangle would keep eating clicks.
      if (modeRef.current !== 'drag' && pointer.current.seen) {
        setInteractive(hitTest(pointer.current.x, pointer.current.y))
      }

      raf = window.requestAnimationFrame(step)
    }

    layout()
    window.addEventListener('resize', layout)
    raf = window.requestAnimationFrame(step)
    return () => {
      window.cancelAnimationFrame(raf)
      window.removeEventListener('resize', layout)
    }
  }, [applyPosition, beginTeleport, hitTest, hop, reportBounds, savePosition, setFacingSafe, setInteractive, setModeSafe])

  // Reaction clips are one-shots; after they finish, the pet returns to its
  // ambient state. The movement loop refuses to start a walk while one plays.
  useEffect(() => {
    reactionRef.current = reaction
    if (reaction !== 'leaving') pendingTeleport.current = null
    if (!reaction) return
    if (modeRef.current === 'walk') setModeSafe('idle')

    if (reaction === 'leaving') {
      const timer = window.setTimeout(() => {
        const target = pendingTeleport.current
        pendingTeleport.current = null
        if (target == null) {
          setReaction(null)
          return
        }
        const p = pos.current
        const turned = target.x < p.x ? 'left' : 'right'
        p.x = clamp(target.x, 0, Math.max(0, window.innerWidth - PET_W))
        p.y = clamp(target.y, 0, p.baseY)
        applyPosition()
        reportBounds(true)
        savePosition()
        streak.current += 1
        // The ribbon is not cleared here: it has to outlive the jump, and she
        // forms out of the landing drawings beneath it. Her own body is held
        // back until those drawings hand over, so nothing pops into being.
        if (!prefersReducedMotion()) {
          setForm({ key: performance.now(), facing: turned })
          setVeiled(true)
          if (veilTimer.current != null) window.clearTimeout(veilTimer.current)
          veilTimer.current = window.setTimeout(() => {
            veilTimer.current = null
            setVeiled(false)
          }, FORM_HANDOFF_MS)
        }
        setReaction('arriving')
      }, REACTION_MS.leaving)
      return () => window.clearTimeout(timer)
    }

    const timer = window.setTimeout(() => {
      if (reaction === 'arriving') setVeiled(false)
      setReaction(null)
      if (reaction === 'arriving') {
        // A teleport landed; standing there politely for three seconds is what
        // makes her look scripted. Chained arrivals come with a short pause.
        idleAt.current =
          performance.now() +
          (streak.current > 0 ? 500 + Math.random() * 1300 : 1100 + Math.random() * 2600)
      }
    }, REACTION_MS[reaction])
    return () => window.clearTimeout(timer)
  }, [applyPosition, reaction, savePosition, setModeSafe])

  useEffect(() => {
    agentRef.current = agent
    if (agent !== 'idle' && modeRef.current === 'walk') {
      setModeSafe('idle')
      idleAt.current = performance.now() + 2000
    }
  }, [agent, setModeSafe])

  useEffect(() => {
    onlineRef.current = online
  }, [online])

  useEffect(() => {
    mouthRef.current = mouth
    if (mouth) {
      if (modeRef.current === 'walk') setModeSafe('idle')
      idleAt.current = performance.now() + 1200
    }
  }, [mouth, setModeSafe])

  useEffect(() => {
    forcedSleepRef.current = forcedSleep
    if (forcedSleep && (modeRef.current === 'walk' || modeRef.current === 'fall')) {
      setModeSafe('idle')
    }
  }, [forcedSleep, setModeSafe])

  useEffect(() => {
    wanderRef.current = wander
    try {
      localStorage.setItem(WANDER_KEY, wander ? 'true' : 'false')
    } catch {
      /* persistence is optional */
    }
    if (!wander && modeRef.current === 'walk') setModeSafe('idle')
  }, [wander, setModeSafe])

  // Staying anchored keeps her where she was put instead of dropping back to
  // the floor. The floor itself is the top of the work area, so this also
  // keeps her out from under the taskbar on any resize or display change.
  useEffect(() => {
    anchoredRef.current = anchored
    try {
      localStorage.setItem(ANCHOR_KEY, anchored ? 'true' : 'false')
    } catch {
      /* persistence is optional */
    }
    if (!anchored || !placed) return
    if (wanderRef.current) setWander(false)
    if (modeRef.current === 'walk' || modeRef.current === 'fall') setModeSafe('idle')
    const p = pos.current
    p.y = clamp(Number.isFinite(p.y) ? p.y : p.baseY, 0, p.baseY)
    applyPosition()
    reportBounds(true)
  }, [anchored, applyPosition, placed, reportBounds, setModeSafe])

  // Events: the panel owns the audio, while both views share agent state.
  useEffect(() => {
    try {
      const saved = localStorage.getItem(WANDER_KEY)
      if (saved === 'false') setWander(false)
    } catch {
      /* default to wandering */
    }

    const offEvent = capri.onEvent((event) => {
      if (event.type === 'core.status') {
        const live = event.state === 'online'
        setOnline(live)
        if (!live) setAgent('sleeping')
        else if (agentRef.current === 'sleeping') setAgent('idle')
      } else if (event.type === 'agent.state') {
        setAgent(moodFor(event.state))
      } else if (event.type === 'avatar.place') {
        // She was told where to stand. Wandering would walk her straight off
        // again, so this counts as being put somewhere, like a drag.
        const maxX = Math.max(0, window.innerWidth - PET_W)
        const margin = Math.min(64, Math.round(maxX / 6))
        const target =
          event.edge === 'left'
            ? margin
            : event.edge === 'right'
              ? maxX - margin
              : Math.round(maxX / 2)
        wanderRef.current = false
        setWander(false)
        beginTeleport(target, pos.current.baseY)
      } else if (event.type === 'error') {
        setAgent('error')
      }
    })
    const offMouth = capri.onPetMouth((viseme) => setMouth(viseme))
    const offListening = capri.onPetListening((active) => setListening(active))
    const offReset = capri.onPetReset(() => {
      interactiveRef.current = false
      pointer.current = { x: 0, y: 0, seen: false }
      reportBounds(true)
    })
    const offCommand = capri.onPetCommand((command) => {
      if (command === 'wander') {
        anchoredRef.current = false
        wanderRef.current = true
        setAnchored(false)
        setWander(true)
        setReaction(null)
        setForcedSleep(false)
        setModeSafe('idle')
        const p = pos.current
        p.y = p.baseY
        applyPosition()
        reportBounds(true)
        savePosition()
      } else if (command === 'anchor' || command === 'stay') {
        anchoredRef.current = true
        wanderRef.current = false
        setAnchored(true)
        setWander(false)
        setReaction(null)
        setModeSafe('idle')
        const p = pos.current
        p.y = clamp(p.y, 0, p.baseY)
        applyPosition()
        reportBounds(true)
        savePosition()
      } else if (command === 'teleport') {
        const maxX = Math.max(0, window.innerWidth - PET_W)
        if (maxX < 24) return
        let target = maxX * (0.08 + Math.random() * 0.3)
        if (pos.current.x < maxX / 2) target = maxX * (0.62 + Math.random() * 0.3)
        if (Math.abs(target - pos.current.x) < 120) target = pos.current.x < maxX / 2 ? maxX : 0
        beginTeleport(
          target,
          Math.random() < 0.5 ? perchY(pos.current.baseY) : pos.current.baseY,
        )
      } else if (command === 'dance') {
        if (forcedSleepRef.current || !onlineRef.current) return
        setModeSafe('idle')
        setReaction('dancing')
      } else if (command === 'sleep') {
        setForcedSleep(true)
        setReaction(null)
        setModeSafe('idle')
      } else if (command === 'wake') {
        setForcedSleep(false)
        setReaction('waking')
      }
    })

    capri.petReady()
    capri.petPointer(false)
    reportBounds(true)

    return () => {
      offEvent()
      offMouth()
      offListening()
      offReset()
      offCommand()
      if (annoyTimer.current !== null) {
        window.clearTimeout(annoyTimer.current)
        annoyTimer.current = null
      }
    }
  }, [applyPosition, beginTeleport, reportBounds, savePosition, setModeSafe])

  // With click-through enabled the window still receives forwarded movement.
  // Only the visible sprite rectangle flips the window back to interactive.
  useEffect(() => {
    const onMove = (event: MouseEvent) => {
      pointer.current = { x: event.clientX, y: event.clientY, seen: true }
      if (modeRef.current === 'drag') {
        setInteractive(true)
        return
      }
      setInteractive(hitTest(event.clientX, event.clientY))
    }
    const onLeave = () => {
      if (modeRef.current !== 'drag') setInteractive(false)
    }
    window.addEventListener('mousemove', onMove)
    document.documentElement.addEventListener('mouseleave', onLeave)
    return () => {
      window.removeEventListener('mousemove', onMove)
      document.documentElement.removeEventListener('mouseleave', onLeave)
    }
  }, [hitTest, setInteractive])

  const onPointerDown = (event: React.PointerEvent<HTMLDivElement>) => {
    if (event.button !== 0 || reactionRef.current === 'leaving') return
    const actor = actorRef.current
    if (!actor) return
    event.preventDefault()
    try {
      actor.setPointerCapture(event.pointerId)
    } catch {
      /* synthetic pointers have no active capture */
    }
    const rect = actor.getBoundingClientRect()
    drag.current = {
      pointerId: event.pointerId,
      offsetX: event.clientX - rect.left,
      offsetY: event.clientY - rect.top,
      startX: event.clientX,
      startY: event.clientY,
      lastX: event.clientX,
      lastY: event.clientY,
      lastAt: performance.now(),
      vx: 0,
      vy: 0,
      moved: false,
    }
    velocity.current = { x: 0, y: 0 }
    setReaction(null)
    setModeSafe('drag')
    setInteractive(true)
    capri.petDrag?.(true)
    applyPosition()
  }

  const onPointerMove = (event: React.PointerEvent<HTMLDivElement>) => {
    const held = drag.current
    if (!held || held.pointerId !== event.pointerId) return
    const now = performance.now()
    const elapsed = Math.max(8, now - held.lastAt)
    held.vx = clamp(((event.clientX - held.lastX) / elapsed) * 1000, -1400, 1400)
    held.vy = clamp(((event.clientY - held.lastY) / elapsed) * 1000, -1000, 1400)
    held.lastX = event.clientX
    held.lastY = event.clientY
    held.lastAt = now
    if (Math.hypot(event.clientX - held.startX, event.clientY - held.startY) > 4) {
      held.moved = true
    }

    const p = pos.current
    p.x = clamp(event.clientX - held.offsetX, 0, Math.max(0, window.innerWidth - PET_W))
    p.y = clamp(event.clientY - held.offsetY, -PET_H * 0.45, window.innerHeight - 36)
    applyPosition()
  }

  const finishPointer = useCallback(
    (event?: React.PointerEvent<HTMLDivElement> | PointerEvent, cancelled = false) => {
      const held = drag.current
      if (!held) return
      drag.current = null
      capri.petDrag?.(false)
      try {
        if (event && 'pointerId' in event) {
          actorRef.current?.releasePointerCapture(event.pointerId)
        }
      } catch {
        /* capture already released */
      }

      if (!cancelled && !held.moved) {
        if (forcedSleepRef.current) {
          setForcedSleep(false)
          setReaction('waking')
        } else if (!poke(performance.now())) {
          setReaction('clicked')
        }
        setModeSafe('idle')
        idleAt.current = performance.now() + 2200
      } else if (anchoredRef.current) {
        const p = pos.current
        p.y = clamp(p.y, 0, p.baseY)
        velocity.current = { x: 0, y: 0 }
        setModeSafe('idle')
        idleAt.current = performance.now() + 2200
        applyPosition()
        savePosition()
      } else {
        velocity.current = {
          x: clamp(held.vx, -900, 900),
          y: clamp(held.vy, -500, 900),
        }
        setModeSafe('fall')
        savePosition()
      }
      const cx = event ? event.clientX : pointer.current.x
      const cy = event ? event.clientY : pointer.current.y
      setInteractive(hitTest(cx, cy))
    },
    [applyPosition, hitTest, poke, savePosition, setInteractive, setModeSafe],
  )

  useEffect(() => {
    const onWindowBlur = () => {
      if (drag.current) finishPointer(undefined, true)
    }
    const onWindowPointerUp = (e: PointerEvent) => {
      if (drag.current) finishPointer(e, false)
    }
    window.addEventListener('blur', onWindowBlur)
    window.addEventListener('pointerup', onWindowPointerUp)
    return () => {
      window.removeEventListener('blur', onWindowBlur)
      window.removeEventListener('pointerup', onWindowPointerUp)
    }
  }, [finishPointer])

  const state =
    mode === 'drag' || mode === 'fall'
      ? 'dragging'
      : reaction === 'clicked'
        ? 'clicked'
        : reaction === 'dancing'
          ? 'dancing'
          : reaction === 'waking'
            ? 'waking'
            : reaction === 'annoyed'
              ? 'annoyed'
              : reaction === 'leaving'
                ? 'leaving'
                : reaction === 'arriving'
                  ? 'arriving'
                  : !online || forcedSleep
                    ? 'sleeping'
                    : mouth
                      ? 'talking'
                      : mode === 'walk'
                        ? 'walking'
                        : agent === 'idle' && listening
                          ? 'listening'
                          : agent
  // A pat on a ledge still gets the grin, and a drop is a drop: the drag and
  // the one-shot reactions stay on the floor art.
  const seated =
    perched && state !== 'dragging' && state !== 'clicked' && state !== 'waking'
  const posed = seated ? perchedState(state) : state
  // The glitch itself is a burst: thinking with no burst showing stands idle.
  const shownState =
    (posed === 'thinking' || posed === 'perched_thinking') && !glitchBurst
      ? seated
        ? 'perched'
        : 'idle'
      : posed

  return (
    <div
      className="pet-root"
      data-mode={mode}
      data-state={state}
      data-wander={wander}
      data-anchored={anchored}
      onPointerMove={onPointerMove}
      onPointerUp={(event) => finishPointer(event)}
      onPointerCancel={(event) => finishPointer(event, true)}
    >
      <div
        ref={actorRef}
        className={`pet-actor${veiled ? ' is-veiled' : ''}`}
        data-placed={placed}
        data-perched={perched}
        role="button"
        tabIndex={-1}
        aria-label="Capri, your desktop companion. Drag to move her, double-click to open her panel."
        title="Capri — drag me, double-click to talk"
        onPointerDown={onPointerDown}
        onLostPointerCapture={() => finishPointer(undefined, true)}
        onDoubleClick={() => void capri.windowControl('show')}
        onContextMenu={(event) => {
          event.preventDefault()
          setInteractive(true)
          capri.petMenu({ wander, sleeping: forcedSleep, anchored })
        }}
      >
        <div className="pet-body">
          <CapriStage
            packName="capri-neon"
            state={shownState}
            mouth={mouth}
            facing={facing}
            growTop={STAGE_RISE}
            growSide={STAGE_SIDE}
            frame={[PET_W, PET_H]}
            onInk={onInk}
            onReady={() => console.log('[capri] pet ready')}
          />
        </div>
        {form ? (
          <FormArrival
            key={form.key}
            petW={PET_W}
            petH={PET_H}
            facing={form.facing}
            onDone={clearForm}
          />
        ) : null}
      </div>
      {ribbon ? (
        <RibbonTrip
          key={ribbon.key}
          fromX={ribbon.fromX}
          toX={ribbon.toX}
          fromY={ribbon.fromY}
          toY={ribbon.toY}
          width={PET_W}
          height={PET_H}
          onDone={clearRibbon}
        />
      ) : null}
    </div>
  )
}
