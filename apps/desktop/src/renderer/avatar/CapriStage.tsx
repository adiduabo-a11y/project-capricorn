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
  onReady?: () => void
}

/**
 * Owns the WebGL stage. Mounts once per pack, then only pushes state changes
 * into the figure — no re-render churn, no context leaks.
 */
export function CapriStage({ packName, state, mouth, facing = 'right', onReady }: Props) {
  const hostRef = useRef<HTMLDivElement | null>(null)
  const figureRef = useRef<CapriFigure | null>(null)
  const readyRef = useRef(onReady)
  const stateRef = useRef(state)
  const mouthRef = useRef(mouth ?? 'closed')
  const facingRef = useRef(facing)

  readyRef.current = onReady
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
        const width = host.clientWidth || 1
        const height = host.clientHeight || 1
        const scale = Math.min((width * 0.96) / pack.canvas[0], (height * 0.99) / pack.canvas[1])
        figure.view.scale.set(scale)
        figure.view.position.set(width / 2, height - 2)
      }

      layout()
      observer = new ResizeObserver(layout)
      observer.observe(host)

      instance.ticker.add((ticker) => {
        figure.update(ticker.deltaMS)
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
