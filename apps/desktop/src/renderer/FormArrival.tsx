/**
 * The landing: Qewiw's nine transformation drawings in `form.png`, played as
 * drawn at twenty-four a second, the way the source project's `Ribbons.cs`
 * plays them. The first five are the ball of ribbon, back to front; the last
 * four are its ribbons alone, and her own body shows under those, so she is
 * revealed at the handoff rather than popping in.
 *
 * The strip's cell is the source animation's 2048 px frame. Everything here is
 * measured in that frame's pixels and scaled by her drawn cell height, so the
 * drawings line up with her body at any size.
 */

import { useEffect, useRef, useState } from 'react'

import formSheet from '@packs/sprites/capri-neon/sprites/form.png'

export const FORM_DRAWINGS = 9
export const FORM_DRAW_MS = 1000 / 24
// Her own window takes over under drawing five.
export const FORM_HANDOFF_MS = 5 * FORM_DRAW_MS

const FRAME = 2048
const CELL_H = 516
const CELL_W = 576
const MIDDLE = { x: 1015, y: 1004 }
// One pixel of the animation, in the pixels of her 384 tall cell.
const IN_CELL = 0.335

interface Props {
  petW: number
  petH: number
  facing: 'left' | 'right'
  onDone: () => void
}

// It mounts inside the actor, so everything is measured from her own box: the
// middle of the ball of ribbon sits on the middle of her body.
export function FormArrival({ petW, petH, facing, onDone }: Props) {
  const [drawing, setDrawing] = useState(0)
  const done = useRef(onDone)
  done.current = onDone

  useEffect(() => {
    const started = performance.now()
    let raf = 0
    const step = () => {
      const elapsed = performance.now() - started
      const index = Math.floor(elapsed / FORM_DRAW_MS)
      if (index >= FORM_DRAWINGS) {
        done.current()
        return
      }
      setDrawing((current) => (current === index ? current : index))
      raf = window.requestAnimationFrame(step)
    }
    raf = window.requestAnimationFrame(step)
    return () => window.cancelAnimationFrame(raf)
  }, [])

  // Her displayed cell is what sizes the drawings; the ball's middle is what
  // lines up with the middle of her body, where the ribbons landed.
  const shownCell = petH * 0.99
  const scale = (IN_CELL * shownCell) / 384
  const cellW = FRAME * scale
  const cellH = (CELL_H / CELL_W) * cellW
  const midX = petW / 2
  const midY = petH * 0.56
  const turned = facing === 'right'

  return (
    <div
      className="form-arrival"
      aria-hidden
      style={{
        left: midX - MIDDLE.x * scale,
        top: midY - MIDDLE.y * scale,
        width: cellW,
        height: cellH,
        backgroundImage: `url(${formSheet})`,
        backgroundSize: `${cellW * FORM_DRAWINGS}px ${cellH}px`,
        backgroundPosition: `${-drawing * cellW}px 0`,
        transform: turned ? 'scaleX(-1)' : undefined,
        transformOrigin: `${MIDDLE.x * scale}px ${MIDDLE.y * scale}px`,
      }}
    />
  )
}
