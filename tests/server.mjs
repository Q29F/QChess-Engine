import assert from 'node:assert/strict'
import { spawn } from 'node:child_process'
import { setTimeout as delay } from 'node:timers/promises'
import { fileURLToPath } from 'node:url'

const projectRoot = fileURLToPath(new URL('../', import.meta.url))

/** Launch the real CLI with an ephemeral loopback port and bounded cleanup. */
export async function startWorkbench(t) {
  const python = process.env.CHESS_PYTHON || (process.platform === 'win32' ? 'python' : 'python3')
  const child = spawn(python, ['-u', 'web.py', '--port', '0'], {
    cwd: projectRoot,
    env: { ...process.env, PYTHONDONTWRITEBYTECODE: '1', PYTHONUNBUFFERED: '1' },
    stdio: ['ignore', 'pipe', 'pipe'],
  })
  let log = ''
  let origin
  let stopped = false
  let spawnError
  const closed = new Promise(resolve => {
    child.once('close', () => { stopped = true; resolve() })
    child.once('error', error => {
      spawnError = error
      if (!child.pid) { stopped = true; resolve() }
    })
  })
  child.stdout.setEncoding('utf8')
  child.stderr.setEncoding('utf8')
  child.stdout.on('data', data => {
    log = (log + data).slice(-16_384)
    origin = /^QChess workbench: (http:\/\/127\.0\.0\.1:[1-9]\d{0,4})\r?$/m.exec(log)?.[1] ?? origin
  })
  child.stderr.on('data', data => { log = (log + data).slice(-16_384) })
  const kill = () => { if (!stopped) child.kill('SIGKILL') }
  process.once('exit', kill)
  t.signal.addEventListener('abort', kill, { once: true })
  t.after(async () => {
    try {
      if (!stopped) {
        child.kill('SIGTERM')
        await Promise.race([closed, delay(2_000, undefined, { ref: false })])
      }
      if (!stopped) {
        kill()
        await Promise.race([closed, delay(2_000, undefined, { ref: false })])
      }
      assert.ok(stopped, 'The local workbench process must stop during cleanup')
    } finally {
      if (stopped) process.removeListener('exit', kill)
      t.signal.removeEventListener('abort', kill)
    }
  })

  const deadline = Date.now() + 30_000
  let readinessError = 'No loopback origin was printed'
  while (Date.now() < deadline && !t.signal.aborted) {
    assert.ok(!stopped, `The local workbench exited before readiness: ${spawnError ?? ''}\n${log}`)
    if (origin) {
      try {
        const response = await fetch(`${origin}/api/health`, {
          redirect: 'error', signal: AbortSignal.any([t.signal, AbortSignal.timeout(1_000)]),
        })
        const body = await response.json()
        if (response.status === 200 && body.status === 'ok' && body.application === 'qchess-workbench') return { origin, python }
        readinessError = `Health returned ${response.status}`
      } catch (error) {
        readinessError = error.message
      }
    }
    await delay(100, undefined, { signal: t.signal })
  }
  assert.fail(`The local workbench was not ready within 30s: ${readinessError}\n${log}`)
}
