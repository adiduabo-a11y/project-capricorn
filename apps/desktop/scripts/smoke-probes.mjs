/**
 * Smoke-mode DOM/UI probes. Loaded dynamically only when Electron runs with --smoke,
 * keeping production ASAR free of test-only probe scripts.
 */

import { writeFileSync } from 'node:fs'
import path from 'node:path'

async function waitForRenderer(win, probe, timeoutMs = 6000) {
  if (!win || win.isDestroyed()) return false
  const deadline = Date.now() + timeoutMs
  while (Date.now() < deadline) {
    const ok = await win.webContents.executeJavaScript(probe).catch(() => false)
    if (ok) return true
    await new Promise((r) => setTimeout(r, 120))
  }
  return false
}

export async function captureFrame(win, outDir, name) {
  try {
    if (!win || win.isDestroyed()) return
    // A transparent window captures as premultiplied alpha, which reads as
    // blown-out when viewed naively. Paint a backdrop for the capture only.
    win.setBackgroundColor('#201e26')
    await new Promise((r) => setTimeout(r, 180))
    const image = await win.webContents.capturePage()
    writeFileSync(path.join(outDir, name), image.toPNG())
    win.setBackgroundColor('#00000000')
    console.log(`[smoke] captured ${name}`)
  } catch (err) {
    console.log(`[smoke] capture skipped: ${err.message}`)
  }
}

async function capturePetFrame(petWin, outDir, name) {
  try {
    if (!petWin || petWin.isDestroyed()) return
    petWin.setBackgroundColor('#201e26')
    await new Promise((r) => setTimeout(r, 180))
    const image = await petWin.webContents.capturePage()
    writeFileSync(path.join(outDir, name), image.toPNG())
    petWin.setBackgroundColor('#00000000')
    console.log(`[smoke] captured ${name}`)
  } catch (err) {
    console.log(`[smoke] pet capture skipped: ${err.message}`)
  }
}

async function probePet(petWin) {
  try {
    if (!petWin || petWin.isDestroyed()) return
    const result = await petWin.webContents.executeJavaScript(`
      (() => {
        const actor = document.querySelector('.pet-actor')
        const root = document.querySelector('.pet-root')
        if (!actor || !root) return { ok: false, reason: 'missing actor' }
        const before = actor.style.transform
        const r = actor.getBoundingClientRect()
        const base = {
          bubbles: true, cancelable: true, pointerId: 77, pointerType: 'mouse',
          isPrimary: true, button: 0, clientX: r.left + 100, clientY: r.top + 150,
        }
        actor.dispatchEvent(new PointerEvent('pointerdown', { ...base, buttons: 1 }))
        actor.dispatchEvent(new PointerEvent('pointermove', {
          ...base, buttons: 1, clientX: r.left + 145, clientY: r.top + 138,
        }))
        const moved = actor.style.transform
        actor.dispatchEvent(new PointerEvent('pointerup', {
          ...base, buttons: 0, clientX: r.left + 145, clientY: r.top + 138,
        }))
        return { ok: true, before, moved, state: root.dataset.state, mode: root.dataset.mode }
      })()
    `)
    console.log(`[smoke] pet probe ${JSON.stringify(result)}`)
  } catch (err) {
    console.log(`[smoke] pet probe skipped: ${err.message}`)
  }
}

async function probePetStates(petWin) {
  try {
    if (!petWin || petWin.isDestroyed()) return
    const pause = () => new Promise((r) => setTimeout(r, 220))
    const read = () =>
      petWin.webContents.executeJavaScript(
        "document.querySelector('.pet-root')?.dataset.state || ''",
      )
    const wait = (ms) => new Promise((r) => setTimeout(r, ms))
    const readX = () =>
      petWin.webContents.executeJavaScript(
        "document.querySelector('.pet-actor')?.getBoundingClientRect().left ?? 0",
      )
    const readY = () =>
      petWin.webContents.executeJavaScript(
        "document.querySelector('.pet-actor')?.getBoundingClientRect().top ?? 0",
      )
    const readMode = () =>
      petWin.webContents.executeJavaScript(
        "document.querySelector('.pet-root')?.dataset.mode || ''",
      )
    const readAnchored = () =>
      petWin.webContents.executeJavaScript(
        "document.querySelector('.pet-root')?.dataset.anchored || ''",
      )
    const readFx = () =>
      petWin.webContents.executeJavaScript(`
        (() => {
          const burst = document.querySelector('.teleport-burst')
          const style = burst ? getComputedStyle(burst) : null
          return {
            burst: Boolean(burst),
            sheet: style ? (style.backgroundImage.includes('click') ? 'click' : 'other') : '',
            size: style ? style.backgroundSize : '',
          }
        })()
      `)
    const command = (name) => petWin.webContents.send('capri:pet-command', name)

    await petWin.webContents.executeJavaScript(`
      (() => {
        const actor = document.querySelector('.pet-actor')
        if (!actor) return
        const r = actor.getBoundingClientRect()
        const base = {
          bubbles: true, cancelable: true, pointerId: 78, pointerType: 'mouse',
          isPrimary: true, button: 0, clientX: r.left + 100, clientY: r.top + 150,
        }
        actor.dispatchEvent(new PointerEvent('pointerdown', { ...base, buttons: 1 }))
        actor.dispatchEvent(new PointerEvent('pointerup', { ...base, buttons: 0 }))
      })()
    `)
    await pause()
    const clicked = await read()
    command('dance')
    await pause()
    const dancing = await read()
    command('sleep')
    await pause()
    const sleeping = await read()
    command('wake')
    await pause()
    const waking = await read()

    const beforeX = await readX()
    command('teleport')
    await pause()
    const leaving = await read()
    const leavingFx = await readFx()
    await wait(700)
    const arriving = await read()
    const arrivingFx = await readFx()
    const afterX = await readX()

    command('anchor')
    await pause()
    const anchorOn = await readAnchored()
    await petWin.webContents.executeJavaScript(`
      (() => {
        const actor = document.querySelector('.pet-actor')
        if (!actor) return
        const r = actor.getBoundingClientRect()
        const startY = r.top + 150
        const endY = Math.max(60, startY - 260)
        const base = {
          bubbles: true, cancelable: true, pointerId: 79, pointerType: 'mouse',
          isPrimary: true, button: 0, clientX: r.left + 100, clientY: startY,
        }
        actor.dispatchEvent(new PointerEvent('pointerdown', { ...base, buttons: 1 }))
        actor.dispatchEvent(new PointerEvent('pointermove', {
          ...base, buttons: 1, clientY: endY,
        }))
        actor.dispatchEvent(new PointerEvent('pointerup', {
          ...base, buttons: 0, clientY: endY,
        }))
      })()
    `)
    await wait(80)
    const droppedY = await readY()
    await wait(450)
    const anchoredY = await readY()
    const anchoredMode = await readMode()
    command('wander')
    await pause()
    const anchorOff = await readAnchored()
    console.log(
      `[smoke] pet states ${JSON.stringify({
        clicked,
        dancing,
        sleeping,
        waking,
        leaving,
        arriving,
        leavingFx,
        arrivingFx,
        teleportMoved: Math.abs(afterX - beforeX) > 10,
        anchor: {
          enabled: anchorOn,
          held: Math.abs(anchoredY - droppedY) < 10,
          mode: anchoredMode,
          released: anchorOff === 'false',
        },
      })}`,
    )
  } catch (err) {
    console.log(`[smoke] pet states skipped: ${err.message}`)
  }
}

async function probePetListening(win, petWin) {
  const report = { ok: false }
  try {
    if (!win || win.isDestroyed() || !petWin || petWin.isDestroyed()) {
      return { ok: false, reason: 'no windows' }
    }
    const settle = (ms = 260) => new Promise((r) => setTimeout(r, ms))
    const command = (name) => petWin.webContents.send('capri:pet-command', name)
    const read = () =>
      petWin.webContents.executeJavaScript(`(() => {
        const root = document.querySelector('.pet-root')
        const avatar = document.querySelector('.pet-actor .avatar')
        const canvas = avatar ? avatar.querySelector('canvas') : null
        return {
          state: (root && root.dataset.state) || '',
          posed: (avatar && avatar.dataset.state) || '',
          filter: canvas ? getComputedStyle(canvas).filter : '',
        }
      })()`)
    const send = (on) =>
      win.webContents.executeJavaScript(
        `window.capri.petListening(${on ? 'true' : 'false'})`,
      )
    const until = async (want, tries = 14) => {
      let seen = await read()
      for (let i = 0; i < tries && seen.state !== want; i += 1) {
        await settle(120)
        seen = await read()
      }
      return seen
    }

    command('anchor')
    let seen = await read()
    for (let i = 0; i < 120 && seen.state === 'talking'; i += 1) {
      await settle(125)
      seen = await read()
    }
    await send(false)
    const cold = await until('idle')

    await send(true)
    const hot = await until('listening')

    petWin.webContents.reload()
    await settle(1200)
    command('anchor')
    const rebooted = await until('listening', 20)

    await send(false)
    const off = await until('idle')
    command('wander')

    Object.assign(report, { cold, hot, rebooted, off })
    report.ok =
      cold.state === 'idle' &&
      !/drop-shadow/.test(cold.filter) &&
      hot.state === 'listening' &&
      hot.posed === 'listening' &&
      /drop-shadow/.test(hot.filter) &&
      rebooted.state === 'listening' &&
      /drop-shadow/.test(rebooted.filter) &&
      off.state === 'idle'
  } catch (err) {
    report.error = err.message
  }
  console.log(`[smoke] pet listening ${JSON.stringify(report)}`)
  return report
}

async function probePerchArt(petWin, outDir, getPetPointerInteractive) {
  const report = { ok: false }
  try {
    if (!petWin || petWin.isDestroyed()) return { ok: false, reason: 'no pet window' }
    const settle = (ms = 300) => new Promise((r) => setTimeout(r, ms))
    const command = (name) => petWin.webContents.send('capri:pet-command', name)
    const read = () =>
      petWin.webContents.executeJavaScript(`(() => {
        const root = document.querySelector('.pet-root')
        const avatar = document.querySelector('.pet-actor .avatar')
        const actor = document.querySelector('.pet-actor')
        const r = actor ? actor.getBoundingClientRect() : null
        return {
          state: (root && root.dataset.state) || '',
          posed: (avatar && avatar.dataset.state) || '',
          top: r ? Math.round(r.top) : 0,
          transform: actor ? actor.style.transform : '',
          innerHeight: window.innerHeight,
        }
      })()`)
    const dragUp = (dy) =>
      petWin.webContents.executeJavaScript(`(() => {
        const actor = document.querySelector('.pet-actor')
        if (!actor) return false
        const r = actor.getBoundingClientRect()
        const base = {
          bubbles: true, cancelable: true, pointerId: 91, pointerType: 'mouse',
          isPrimary: true, button: 0, clientX: r.left + 100, clientY: r.top + 150,
        }
        actor.dispatchEvent(new PointerEvent('pointerdown', { ...base, buttons: 1 }))
        actor.dispatchEvent(new PointerEvent('pointermove', {
          ...base, buttons: 1, clientY: r.top + 150 - ${dy},
        }))
        actor.dispatchEvent(new PointerEvent('pointerup', {
          ...base, buttons: 0, clientY: r.top + 150 - ${dy},
        }))
        return true
      })()`)

    command('anchor')
    await settle(400)
    const before = await read()
    await dragUp(300)
    await settle(700)
    const perched = await read()
    await capturePetFrame(petWin, outDir, '.smoke-perch.png')

    const box = await petWin.webContents.executeJavaScript(`(() => {
      const actor = document.querySelector('.pet-actor')
      if (!actor) return null
      const r = actor.getBoundingClientRect()
      return { left: r.left, top: r.top, width: r.width, height: r.height }
    })()`)
    const legX = Math.round(box.left + box.width / 2)
    const legY = Math.round(box.top + box.height + 120)
    const moveTo = async (x, y, want) => {
      for (let i = 0; i < 8; i += 1) {
        petWin.webContents.sendInputEvent({ type: 'mouseMove', x, y })
        await settle(80)
        if (getPetPointerInteractive() === want) return true
      }
      return false
    }
    const legsReachable = await moveTo(legX, legY, true)
    const perchedAttr = await petWin.webContents.executeJavaScript(
      "document.querySelector('.pet-actor')?.dataset.perched || ''",
    )
    const awayClear = await moveTo(12, 12, false)
    const legTarget = await petWin.webContents.executeJavaScript(
      `(() => {
        const el = document.elementFromPoint(${legX}, ${legY})
        return el ? String(el.className || el.tagName) : ''
      })()`,
    )
    petWin.webContents.sendInputEvent({ type: 'mouseMove', x: legX, y: legY })
    await settle(120)
    petWin.webContents.sendInputEvent({
      type: 'mouseDown', x: legX, y: legY, button: 'left', clickCount: 1,
    })
    await settle(120)
    petWin.webContents.sendInputEvent({
      type: 'mouseMove', x: legX, y: legY - 90, button: 'left',
    })
    await settle(160)
    petWin.webContents.sendInputEvent({
      type: 'mouseUp', x: legX, y: legY - 90, button: 'left', clickCount: 1,
    })
    await settle(500)
    const lifted = await read()

    command('sleep')
    await settle(700)
    const asleep = await read()
    await capturePetFrame(petWin, outDir, '.smoke-perch-sleep.png')

    command('wake')
    await settle(500)
    command('wander')

    Object.assign(report, {
      before,
      perched,
      legs: {
        x: legX,
        y: legY,
        reachable: legsReachable,
        perchedAttr,
        awayClear,
        target: legTarget,
        liftedTop: lifted.top,
      },
      asleep,
    })
    report.ok =
      perched.posed.startsWith('perched') &&
      asleep.posed === 'perched_sleeping' &&
      legsReachable === true &&
      awayClear === true &&
      legTarget.includes('pet-actor') &&
      lifted.top < perched.top - 10
  } catch (err) {
    report.error = err.message
  }
  console.log(`[smoke] perch art ${JSON.stringify(report)}`)
  return report
}

async function probeMicRouteUi(win) {
  try {
    if (!win || win.isDestroyed()) return
    await win.webContents.executeJavaScript(`
      (() => {
        const tab = [...document.querySelectorAll('.rail-tab')]
          .find((node) => node.textContent.includes('MAINFRAME'))
        tab?.click()
        return Boolean(tab)
      })()
    `)
    await waitForRenderer(win, `!!document.querySelector('.mic-route')`, 2000)
    const result = await win.webContents.executeJavaScript(`
      (() => {
        const section = document.querySelector('.mic-route')
        return {
          present: Boolean(section),
          title: section?.querySelector('.mic-route-title')?.textContent || '',
          phase: section?.dataset.phase || '',
          toggle: Boolean(section?.querySelector('input[type=checkbox]')),
          selects: section?.querySelectorAll('select').length || 0,
        }
      })()
    `)
    console.log(`[smoke] mic route ${JSON.stringify(result)}`)
  } catch (err) {
    console.log(`[smoke] mic route probe skipped: ${err.message}`)
  }
}

async function probeBubble(win) {
  try {
    if (!win || win.isDestroyed()) return
    await win.webContents.executeJavaScript("window.capri.setUiMode('bubble')")
    await waitForRenderer(win, `!!document.querySelector('.qb-card')`, 2000)
    const result = await win.webContents.executeJavaScript(`
      (() => {
        const card = document.querySelector('.qb-card')
        if (!card) return { present: false }
        return {
          present: true,
          side: card.closest('.bubble-shell')?.dataset.side || '',
          messages: card.querySelectorAll('.qb-msg').length,
          input: Boolean(card.querySelector('textarea')),
          actions: card.querySelectorAll('.qb-icon').length,
        }
      })()
    `)
    const bubbleBounds = win.getBounds()
    console.log(
      `[smoke] bubble ${JSON.stringify({
        ...result,
        window: { width: bubbleBounds.width, height: bubbleBounds.height },
        resizable: win.isResizable(),
      })}`,
    )

    await win.webContents.executeJavaScript("window.capri.setUiMode('panel')")
    const expanded = await waitForRenderer(win, `!!document.querySelector('.shell')`, 2000)
    const panelBounds = win.getBounds()
    console.log(
      `[smoke] bubble expand ${JSON.stringify({
        expanded,
        window: { width: panelBounds.width, height: panelBounds.height },
        resizable: win.isResizable(),
      })}`,
    )
  } catch (err) {
    console.log(`[smoke] bubble probe skipped: ${err.message}`)
  }
}

async function probePoseScale(petWin, outDir) {
  const pause = (ms) => new Promise((r) => setTimeout(r, ms))
  const readState = () =>
    petWin.webContents.executeJavaScript(
      "document.querySelector('.pet-root')?.dataset.state || ''",
    )
  const geometry = () =>
    petWin.webContents.executeJavaScript(`(() => {
      const actor = document.querySelector('.pet-actor')
      const canvas = document.querySelector('.pet-actor .avatar canvas')
      if (!actor || !canvas) return null
      const a = actor.getBoundingClientRect()
      const c = canvas.getBoundingClientRect()
      return {
        state: document.querySelector('.pet-root')?.dataset.state || '',
        posed: document.querySelector('.pet-actor .avatar')?.dataset.state || '',
        actor: [Math.round(a.left), Math.round(a.top), Math.round(a.width), Math.round(a.height)],
        canvas: [Math.round(c.left), Math.round(c.top), Math.round(c.width), Math.round(c.height)],
      }
    })()`)
  const command = (name) => petWin.webContents.send('capri:pet-command', name)
  const waitState = async (name, timeout = 3000) => {
    const deadline = Date.now() + timeout
    while (Date.now() < deadline) {
      if ((await readState()) === name) return true
      await pause(60)
    }
    return false
  }
  const wideHits = []
  const shoot = async (name, note) => {
    const info = await geometry()
    const hit = await petWin.webContents.executeJavaScript(
      "document.querySelector('.pet-actor')?.dataset.hit ?? ''",
    )
    const width = Number(String(hit).split(',')[2])
    if (!(width > 0) || width > 310) wideHits.push(`${name}:${hit || 'none'}`)
    await capturePetFrame(petWin, outDir, `.smoke-pose-${name}.png`)
    console.log(`[smoke] pose ${name} ${JSON.stringify({ ...info, hit, ...note })}`)
  }

  try {
    if (!petWin || petWin.isDestroyed()) return { ok: false }

    command('wake')
    await waitState('waking', 2000)
    await pause(1200)
    command('anchor')
    await pause(250)
    let walking = false
    const walkDeadline = Date.now() + 20_000
    while (!walking && Date.now() < walkDeadline) {
      command('wander')
      walking = await waitState('walking', 2500)
    }
    await pause(120)
    await shoot('walk', { seen: walking })

    command('anchor')
    await pause(250)
    const g = await geometry()
    const gripX = Math.round(g.actor[0] + g.actor[2] / 2)
    const gripY = Math.round(g.actor[1] + g.actor[3] * 0.3)
    petWin.webContents.sendInputEvent({ type: 'mouseMove', x: gripX, y: gripY })
    await pause(120)
    petWin.webContents.sendInputEvent({ type: 'mouseDown', x: gripX, y: gripY, button: 'left', clickCount: 1 })
    await pause(150)
    petWin.webContents.sendInputEvent({ type: 'mouseMove', x: gripX, y: gripY - 40, button: 'left' })
    const dragging = await waitState('dragging', 2000)
    await pause(150)
    await shoot('drag', { seen: dragging })
    petWin.webContents.sendInputEvent({ type: 'mouseUp', x: gripX, y: gripY - 40, button: 'left', clickCount: 1 })
    await pause(500)

    command('anchor')
    await pause(200)
    command('dance')
    const dancing = await waitState('dancing', 3000)
    await pause(250)
    await shoot('dance', { seen: dancing })

    command('sleep')
    const sleeping = await waitState('sleeping', 3000)
    await pause(350)
    await shoot('sleep', { seen: sleeping })

    command('wake')
    const waking = await waitState('waking', 3000)
    await pause(100)
    await shoot('wake', { seen: waking })

    await pause(900)
    const hideOverlays = () =>
      petWin.webContents.executeJavaScript(`(() => {
        const stray = document.querySelectorAll(
          '.pet-root > *:not(.pet-actor), .teleport-burst, [class*="ribbon"], [class*="burst"]',
        )
        for (const node of stray) node.style.setProperty('visibility', 'hidden')
        return stray.length
      })()`)
    command('teleport')
    const leaving = await waitState('leaving', 3000)
    const hidden = await hideOverlays()
    await pause(50)
    await shoot('leave', { seen: leaving })
    console.log(`[smoke] leave overlays hidden: ${hidden}`)

    await petWin.webContents.executeJavaScript(`(() => {
      const root = document.querySelector('.pet-root')
      if (!root) return
      for (const child of root.children) child.style.removeProperty('visibility')
    })()`)

    await pause(1400)
    command('anchor')
    await pause(400)
    return {
      ok: dragging && dancing && waking && leaving && wideHits.length === 0,
      walking,
      dragging,
      dancing,
      waking,
      leaving,
      wideHits,
    }
  } catch (err) {
    return { ok: false, error: err.message }
  }
}

export async function runSmokeSuite({
  win,
  petWin,
  outDir,
  finishSmoke,
  getPetPointerInteractive,
}) {
  const settled = await waitForRenderer(
    win,
    `
    !!document.querySelector('.msg.assistant p') &&
    document.querySelector('.msg.assistant p').innerText.length > 8 &&
    !document.querySelector('.confirm') &&
    document.querySelectorAll('.tool').length >= 2
  `,
  )
  if (!settled) {
    console.log('[smoke] trace did not settle')
    finishSmoke(1)
    return
  }

  await probePet(petWin)
  await captureFrame(win, outDir, '.smoke.png')
  await capturePetFrame(petWin, outDir, '.smoke-pet.png')
  await probePetStates(petWin)
  const listening = await probePetListening(win, petWin)
  if (!listening.ok) {
    console.log('[smoke] pet listening failed')
    finishSmoke(1)
    return
  }
  const perch = await probePerchArt(petWin, outDir, getPetPointerInteractive)
  if (!perch.ok) {
    console.log('[smoke] perch art failed')
    finishSmoke(1)
    return
  }
  const poses = await probePoseScale(petWin, outDir)
  if (!poses.ok) {
    console.log(`[smoke] pose scale failed ${JSON.stringify(poses)}`)
    finishSmoke(1)
    return
  }
  await probeMicRouteUi(win)
  await probeBubble(win)
  console.log('[smoke] ok')
  finishSmoke(0)
}
