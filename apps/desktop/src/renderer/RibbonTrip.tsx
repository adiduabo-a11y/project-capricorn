/**
 * The teleport ribbons.
 *
 * Ported in spirit from the source project's `Ribbons.cs`: she does not slide
 * across the desktop, she comes apart into strands of her own colours - hair
 * red with white flecks, thin green lines - that wind into a ball where she
 * stood and then stream to where she will land. The strands are what show the
 * destination: they reach it before she does.
 *
 * This draws the strands afresh every trip, like the original; it does not move
 * a picture of her. The pack's arrival clip still plays her forming at the far
 * end, so no extra artwork is needed.
 */

import { useEffect, useMemo } from 'react'

const HAIR = '#ff2050'
const ACCENT = '#32f06a'
const FLECK = '#f6eef2'

interface Strand {
  hair: boolean
  width: number
  amp: number
  delay: number
  opacity: number
}

// Six strands: four of hers and two green. Fewer, fatter ribbons rather than a
// fan of thin lines - each one is wide enough to read as a tear in her own
// colours rather than a drawn arrow. Delays stay short so the whole bundle is
// airborne at once - the trip is a snap across the desktop, not a procession.
const STRANDS: Strand[] = [
  { hair: true, width: 7.2, amp: 0.10, delay: 0, opacity: 0.95 },
  { hair: false, width: 2.2, amp: 0.05, delay: 20, opacity: 0.9 },
  { hair: true, width: 6.0, amp: 0.16, delay: 45, opacity: 0.9 },
  { hair: true, width: 4.6, amp: 0.07, delay: 70, opacity: 0.88 },
  { hair: false, width: 1.8, amp: 0.035, delay: 10, opacity: 0.8 },
  { hair: true, width: 7.6, amp: 0.12, delay: 30, opacity: 0.92 },
]

interface Props {
  fromX: number
  toX: number
  fromY: number
  toY: number
  width: number
  height: number
  onDone: () => void
}

function middle(x: number, topY: number, width: number, height: number): { x: number; y: number } {
  return { x: x + width / 2, y: topY + height * 0.56 }
}

// A short double loop where the ball forms, then the long bow to the landing.
function strandPath(
  start: { x: number; y: number },
  end: { x: number; y: number },
  index: number,
  amp: number,
): string {
  const dx = end.x - start.x
  const dy = end.y - start.y
  const distance = Math.hypot(dx, dy) || 1
  const px = -dy / distance
  const py = dx / distance
  const direction = index % 2 === 0 ? 1 : -1
  const bow = distance * amp * direction

  const loop = (radius: number, side: number) =>
    `C ${start.x + side * radius} ${start.y - radius * 1.5} ` +
    `${start.x - side * radius * 1.4} ${start.y + radius * 0.9} ` +
    `${start.x + side * radius * 0.2} ${start.y + radius * 0.35}`

  const coil = `${loop(26 + index * 3, direction)} ${loop(13 + index, -direction)}`
  const control1 = {
    x: start.x + dx * 0.3 + px * bow,
    y: start.y + dy * 0.3 + py * bow,
  }
  const control2 = {
    x: start.x + dx * 0.72 - px * bow * 0.6,
    y: start.y + dy * 0.72 - py * bow * 0.6,
  }
  return (
    `M ${start.x} ${start.y} ${coil} ` +
    `C ${control1.x} ${control1.y} ${control2.x} ${control2.y} ${end.x} ${end.y}`
  )
}

export function RibbonTrip({ fromX, toX, fromY, toY, width, height, onDone }: Props) {
  const from = middle(fromX, fromY, width, height)
  const to = middle(toX, toY, width, height)
  const paths = useMemo(
    () =>
      STRANDS.map((strand, index) => ({
        ...strand,
        d: strandPath(from, to, index, strand.amp),
      })),
    // The trip is remounted per teleport, so its geometry never changes under it.
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [],
  )

  // The strands land at 500 ms and she forms from the drawings 40 ms later, so
  // the trip only has to stay up until its landing pulse has faded - 860 ms.
  useEffect(() => {
    const timer = window.setTimeout(onDone, 860)
    return () => window.clearTimeout(timer)
  }, [onDone])

  const scale = height / 300

  return (
    <svg
      className="ribbon-trip"
      width="100%"
      height="100%"
      aria-hidden
      focusable="false"
    >
      <g className="ribbon-ball">
        <circle
          cx={from.x}
          cy={from.y}
          r={36 * scale}
          fill="none"
          stroke={HAIR}
          strokeWidth={2.6 * scale}
        />
        <circle
          cx={from.x}
          cy={from.y}
          r={22 * scale}
          fill="none"
          stroke={ACCENT}
          strokeWidth={1.6 * scale}
        />
      </g>

      {paths.map((strand, index) => (
        <g key={index} opacity={strand.opacity}>
          <path
            className="ribbon-strand"
            d={strand.d}
            pathLength={1}
            fill="none"
            stroke={strand.hair ? HAIR : ACCENT}
            strokeWidth={strand.width * scale}
            strokeLinecap="round"
            style={{ animationDelay: `${strand.delay}ms` }}
          />
          {strand.hair ? (
            <path
              className="ribbon-flecks"
              d={strand.d}
              pathLength={1}
              fill="none"
              stroke={FLECK}
              // Flecks stay thin however wide the ribbon gets: they are specks
              // on it, not a stripe through it.
              strokeWidth={Math.min(1.4, Math.max(0.7, strand.width * 0.18)) * scale}
              strokeLinecap="round"
              style={{ animationDelay: `${strand.delay}ms` }}
            />
          ) : null}
        </g>
      ))}

      <g className="ribbon-land">
        <ellipse
          cx={to.x}
          cy={to.y + height * 0.36}
          rx={30 * scale}
          ry={6 * scale}
          fill={ACCENT}
        />
        <circle
          cx={to.x}
          cy={to.y}
          r={20 * scale}
          fill="none"
          stroke={HAIR}
          strokeWidth={2 * scale}
        />
      </g>
    </svg>
  )
}
