import { useEffect, useRef } from 'react'
import { Application } from 'pixi.js'

// Installs non-eval polyfills for Pixi's generated uniform/shader code.
// Required under a CSP without 'unsafe-eval' — the module name is misleading.
import 'pixi.js/unsafe-eval'

import { createCapri, type CapriFigure } from './placeholder'
import { loadPack } from './pack'
import { createSpriteCapri } from './sprite'

interface Props {
  packName: string
  state: string
  /** Current viseme from the speech player; only the talking state uses it. */
  mouth?: string | null
  facing?: 'left' | 'right'
  /**
   * Extra canvas space above and to the sides of the frame, in CSS px. The
   * standing clips carry a larger clip scale than the perch art, so they rise
   * above the actor box and reach past its sides; the canvas has to exist out
   * there or Pixi clips them at the element edge.
   */
  growTop?: number
  growSide?: number
  /**
   * The actor box the pack canvas is fitted to. Defaults to the host, which is
   * what the panel stage wants; the pet passes 200x300 so growing the canvas
   * for the seated and standing art does not also magnify her.
   */
  frame?: [number, number]
  /**
   * The drawn art for the current pose, in CSS px relative to the actor box's
   * top-left. Fires whenever it changes; null before the first frame.
   */
  onInk?: (rect: { left: number; top: number; width: number; height: number } | null) => void
  onReady?: () => void
}

/**
 * Owns the WebGL stage. Mounts once per pack, then only pushes state changes
 * into the figure — no re-render churn, no context leaks.
 */
export function CapriStage({
  packName,
  state,
  mouth,
  facing = 'right',
  growTop = 0,
  growSide = 0,
  frame,
  onInk,
  onReady,
}: Props) {
  const hostRef = useRef<HTMLDivElement | null>(null)
  const figureRef = useRef<CapriFigure | null>(null)
  const readyRef = useRef(onReady)
  const inkRef = useRef(onInk)
  const stateRef = useRef(state)
  const mouthRef = useRef(mouth ?? 'closed')
  const facingRef = useRef(facing)

  readyRef.current = onReady
  inkRef.current = onInk
  stateRef.current = state
  mouthRef.current = mouth ?? 'closed'
  facingRef.current = facing

  useEffect(() => {
    const host = hostRef.current
    if (!host) return

    let disposed = false
    let app: Application | null = null
    let observer: ResizeObserver | null = null
    let frames = 0

    const pack = loadPack(packName)

    void (async () => {
      const instance = new Application()
      app = instance

      await instance.init({
        backgroundAlpha: 0,
        antialias: true,
        resolution: Math.min(window.devicePixelRatio || 1, 2),
        autoDensity: true,
        resizeTo: host,
        preference: 'webgl',
      })

      if (disposed) {
        instance.destroy(true, { children: true })
        return
      }

      host.appendChild(instance.canvas)

      const figure =
        pack.provider === 'sprite' ? await createSpriteCapri(pack) : createCapri(pack)
      if (disposed) {
        figure.view.destroy({ children: true })
        instance.destroy(true, { children: true })
        return
      }
      figureRef.current = figure
      figure.setState(stateRef.current)
      figure.setMouth(mouthRef.current)
      figure.setFacing(facingRef.current)
      instance.stage.addChild(figure.view)

      const layout = () => {
        // The fit is measured against the actor box, never the canvas element:
        // the pet grows the canvas to hold taller art, and fitting to that would
        // magnify her by the same amount.
        const frameW = frame?.[0] ?? (host.clientWidth || 1)
        const frameH = frame?.[1] ?? Math.max(1, host.clientHeight || 1)
        const scale = Math.min((frameW * 0.96) / pack.canvas[0], (frameH * 0.99) / pack.canvas[1])
        figure.view.scale.set(scale)
        // The anchor is the feet line: the actor's bottom edge, growTop pixels
        // into the canvas.
        figure.view.position.set(growSide + frameW / 2, growTop + frameH - 2)
      }

      layout()
      observer = new ResizeObserver(layout)
      observer.observe(host)

      // Report the drawn art only when it actually moves: the pet's clickable
      // region and the window's click-through follow it.
      let lastInk = ''
      instance.ticker.add((ticker) => {
        figure.update(ticker.deltaMS)
        const ink = figure.inkRect?.() ?? null
        const next = ink
          ? [
              Math.round(ink.x - growSide),
              Math.round(ink.y - growTop),
              Math.round(ink.width),
              Math.round(ink.height),
            ].join(',')
          : ''
        if (next !== lastInk) {
          lastInk = next
          inkRef.current?.(
            ink
              ? {
                  left: ink.x - growSide,
                  top: ink.y - growTop,
                  width: ink.width,
                  height: ink.height,
                }
              : null,
          )
        }
        frames += 1
        if (frames === 2) {
          console.log(`[capri] avatar ready (${pack.name})`)
          readyRef.current?.()
        }
      })
    })().catch((err: unknown) => {
      console.error('[capri] avatar failed to start', err)
    })

    return () => {
      disposed = true
      figureRef.current = null
      observer?.disconnect()
      if (app) {
        app.destroy(true, { children: true })
        app = null
      }
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [packName])

  useEffect(() => {
    figureRef.current?.setState(state)
  }, [state])

  useEffect(() => {
    figureRef.current?.setMouth(mouth ?? 'closed')
  }, [mouth])

  useEffect(() => {
    figureRef.current?.setFacing(facing)
  }, [facing])

  return (
    <div
      className="avatar"
      ref={hostRef}
      aria-label="Capri projection"
      data-state={state}
      data-mouth={mouth ?? 'closed'}
    />
  )
}
