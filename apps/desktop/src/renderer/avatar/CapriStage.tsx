import { useEffect, useRef } from 'react'
import { Application } from 'pixi.js'

// Installs non-eval polyfills for Pixi's generated uniform/shader code.
// Required under a CSP without 'unsafe-eval' — the module name is misleading.
import 'pixi.js/unsafe-eval'

import { createCapri, type CapriFigure } from './placeholder'
import { loadPack } from './pack'

interface Props {
  packName: string
  state: string
  onReady?: () => void
}

/**
 * Owns the WebGL stage. Mounts once per pack, then only pushes state changes
 * into the figure — no re-render churn, no context leaks.
 */
export function CapriStage({ packName, state, onReady }: Props) {
  const hostRef = useRef<HTMLDivElement | null>(null)
  const figureRef = useRef<CapriFigure | null>(null)
  const readyRef = useRef(onReady)

  readyRef.current = onReady

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

      const figure = createCapri(pack)
      figureRef.current = figure
      figure.setState(state)
      instance.stage.addChild(figure.view)

      const layout = () => {
        const width = host.clientWidth || 1
        const height = host.clientHeight || 1
        const scale = Math.min((width * 0.92) / pack.canvas[0], height / pack.canvas[1])
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

  return <div className="avatar" ref={hostRef} aria-label="Capri projection" />
}
