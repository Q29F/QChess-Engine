// Install Python requirements and Playwright Chromium before running this file.
import assert from 'node:assert/strict'
import { Buffer } from 'node:buffer'
import { spawnSync } from 'node:child_process'
import { mkdir } from 'node:fs/promises'
import { join } from 'node:path'
import { test } from 'node:test'
import { chromium } from 'playwright'
import { startWorkbench } from './server.mjs'

const STARTING_FEN = 'rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1'
const CASTLING = 'r3k2r/8/8/8/8/8/8/R3K2R w KQkq - 0 1'
const PROMOTION = '7k/P7/8/8/8/8/8/4K3 w - - 0 1'
const FIFTY = '4k3/8/8/8/8/8/1R6/4K3 w - - 99 80'

function square(page, name) {
  return page.getByRole('button', { name: new RegExp(`^${name},`) })
}

async function waitForPosition(page, fen) {
  const field = await page.getByLabel('Current FEN', { exact: true }).elementHandle()
  try {
    await page.waitForFunction(({ field, fen }) => field.value === fen, { field, fen })
  } finally {
    await field.dispose()
  }
  await page.locator('[role="grid"][aria-busy="false"]').waitFor({ state: 'visible' })
}

async function act(page, endpoint, action) {
  const ready = page.waitForResponse(response => new URL(response.url()).pathname === endpoint && response.request().method() === 'POST')
  await action()
  const response = await ready
  assert.equal(response.status(), 200)
  const result = await response.json()
  await waitForPosition(page, (result.nextState ?? result).fen)
  return { result, request: response.request().postDataJSON() }
}

async function move(page, from, to) {
  return (await act(page, '/api/state', async () => {
    await square(page, from).click()
    await square(page, to).click()
  })).result
}

async function newGame(page, manual = true) {
  const { result } = await act(page, '/api/state', () => page.getByRole('button', { name: 'New game', exact: true }).click())
  assert.equal(result.fen, STARTING_FEN)
  if (manual) await page.getByLabel('Automatic Black reply', { exact: true }).uncheck()
  return result
}

async function loadPosition(page, fen) {
  await page.getByLabel('Load FEN', { exact: true }).fill(fen)
  return (await act(page, '/api/state', () => page.getByRole('button', { name: 'Load position', exact: true }).click())).result
}

async function downloadPgn(page) {
  const ready = page.waitForEvent('download')
  await page.getByRole('button', { name: 'Download PGN', exact: true }).click()
  const artifact = await ready
  assert.equal(artifact.suggestedFilename(), 'qchess-game.pgn')
  assert.equal(await artifact.failure(), null)
  const stream = await artifact.createReadStream()
  assert.ok(stream, 'The download must contain readable data')
  const chunks = []
  for await (const chunk of stream) chunks.push(chunk)
  return Buffer.concat(chunks).toString('utf8')
}

function parsePgn(python, text) {
  const script = [
    'import io, json, sys, chess.pgn',
    'game = chess.pgn.read_game(io.StringIO(sys.stdin.read()))',
    'assert game is not None and not game.errors',
    'print(json.dumps({"initialFen": game.board().fen(), "fen": game.end().board().fen(), "moves": [move.uci() for move in game.mainline_moves()], "result": game.headers["Result"], "comment": game.end().comment}))',
  ].join('\n')
  const parsed = spawnSync(python, ['-c', script], {
    input: text, encoding: 'utf8', timeout: 5_000, maxBuffer: 64_000,
    env: { ...process.env, PYTHONDONTWRITEBYTECODE: '1' },
  })
  assert.equal(parsed.error, undefined)
  assert.equal(parsed.status, 0, parsed.stderr)
  assert.equal(parsed.stderr, '')
  return JSON.parse(parsed.stdout)
}

async function assertExport(page, python, expected) {
  const text = await downloadPgn(page)
  assert.equal(text, expected.pgn)
  const parsed = parsePgn(python, text)
  assert.equal(parsed.initialFen, expected.initialFen)
  assert.equal(parsed.fen, expected.fen)
  assert.deepEqual(parsed.moves, expected.moves)
  assert.equal(parsed.result, expected.outcome?.result ?? '*')
  return parsed
}

async function screenshot(page, filename) {
  if (!process.env.CHESS_SCREENSHOTS) return
  await mkdir(process.env.CHESS_SCREENSHOTS, { recursive: true })
  await page.evaluate(() => globalThis.scrollTo({ top: 0, left: 0, behavior: 'instant' }))
  await page.screenshot({ path: join(process.env.CHESS_SCREENSHOTS, filename), fullPage: true })
}

/** Hold a real completed response until the user has changed the page. */
async function holdResponse(page, origin, endpoint, start, change) {
  let release
  let resolveReady
  let rejectReady
  let finish
  let failure
  let responseHeld = false
  const gate = new Promise(resolve => { release = resolve })
  const ready = new Promise((resolve, reject) => { resolveReady = resolve; rejectReady = reject })
  const done = new Promise(resolve => { finish = resolve })
  const handler = async route => {
    try {
      const response = await route.fetch({ timeout: 6_000 })
      assert.equal(response.status(), 200)
      const body = await response.body()
      responseHeld = true
      resolveReady(JSON.parse(body.toString('utf8')))
      await gate
      await route.fulfill({ response, body })
    } catch (error) {
      if (!responseHeld) rejectReady(error)
      else if (!route.request().failure()) failure = error
    } finally {
      finish()
    }
  }
  await page.route(`${origin}${endpoint}`, handler)
  try {
    await start()
    const old = await ready
    const expected = await change(old)
    release()
    await done
    if (failure) throw failure
    await page.waitForLoadState('networkidle')
    return { old, expected }
  } finally {
    release()
    await page.unroute(`${origin}${endpoint}`, handler)
  }
}

async function holdSearch(page, origin, change) {
  const { old, expected } = await holdResponse(page, origin, '/api/search',
    () => page.getByRole('button', { name: 'Engine move', exact: true }).click(),
    async result => {
      assert.ok(await page.getByRole('button', { name: 'Download PGN', exact: true }).isDisabled())
      return change(result)
    },
  )
  await waitForPosition(page, expected.fen)
  assert.equal(await page.getByLabel('Current FEN', { exact: true }).inputValue(), expected.fen)
  assert.notEqual(expected.fen, old.nextState.fen)
  return expected
}

test('QChess workbench in Chromium', { timeout: 120_000 }, async t => {
  const { origin, python } = await startWorkbench(t)
  const browser = await chromium.launch({ headless: true })
  t.after(() => browser.close())
  const pageErrors = []
  const consoleErrors = []
  const policyViolations = []
  const thirdPartyRequests = []
  const dialogs = []
  const stateRequests = []
  const searchRequests = []

  async function createContext(options = {}) {
    const context = await browser.newContext({ acceptDownloads: true, ...options })
    context.setDefaultTimeout(5_000)
    context.setDefaultNavigationTimeout(15_000)
    await context.exposeBinding('recordPolicyViolation', (_source, details) => policyViolations.push(details))
    await context.addInitScript(() => {
      globalThis.document.addEventListener('securitypolicyviolation', event => {
        globalThis.recordPolicyViolation({ directive: event.effectiveDirective, blocked: event.blockedURI })
      })
    })
    context.on('request', request => {
      if (!/^https?:/.test(request.url())) return
      const url = new URL(request.url())
      if (url.origin !== origin) thirdPartyRequests.push(request.url())
      if (url.pathname === '/api/state' && request.method() === 'POST') stateRequests.push(request)
      if (url.pathname === '/api/search' && request.method() === 'POST') searchRequests.push(request)
    })
    context.on('page', page => {
      page.on('pageerror', error => pageErrors.push(error.message))
      page.on('console', message => {
        if (message.type() === 'error') consoleErrors.push(message.text())
      })
      page.on('dialog', async dialog => {
        dialogs.push(dialog.message())
        await dialog.dismiss()
      })
    })
    return context
  }

  const context = await createContext({ viewport: { width: 1440, height: 1100 } })
  const page = await context.newPage()

  await t.test('loads the standard position with labelled native controls', async () => {
    const response = await page.goto(origin)
    assert.equal(response.status(), 200)
    await waitForPosition(page, STARTING_FEN)
    await page.waitForLoadState('networkidle')
    assert.ok(await page.getByRole('heading', { name: 'QChess workbench', level: 1 }).isVisible())
    const board = page.getByRole('grid', { name: 'Chessboard, White at bottom', exact: true })
    assert.equal(await board.getByRole('button').count(), 64)
    assert.equal(await board.getByRole('button').evaluateAll(buttons => buttons.filter(button => button.tabIndex === 0).length), 1)
    assert.ok(await page.getByLabel('Automatic Black reply', { exact: true }).isChecked())
    assert.equal(await page.getByLabel('Depth', { exact: true }).inputValue(), '2')
    assert.ok(await page.getByLabel('Load FEN', { exact: true }).isEditable())
    assert.ok(!(await page.getByLabel('Current FEN', { exact: true }).isEditable()))
    assert.ok(await page.getByRole('button', { name: 'Undo move', exact: true }).isDisabled())
    assert.ok(!(await page.getByRole('button', { name: 'Download PGN', exact: true }).isDisabled()))
    assert.equal(searchRequests.length, 0)
    const before = stateRequests.length
    await square(page, 'e2').click()
    await square(page, 'e5').click()
    assert.equal(await page.getByLabel('Current FEN', { exact: true }).inputValue(), STARTING_FEN)
    assert.equal(stateRequests.length, before, 'An illegal destination must not submit a move')
    await square(page, 'e2').click()
  })

  await t.test('a human move receives one legal engine reply and exports the current PGN', async () => {
    const ready = page.waitForResponse(response => new URL(response.url()).pathname === '/api/search' && response.request().method() === 'POST')
    await square(page, 'e2').click()
    await square(page, 'e4').click()
    const response = await ready
    assert.equal(response.status(), 200)
    const result = await response.json()
    assert.deepEqual(result.moves, ['e2e4'])
    assert.equal(response.request().postDataJSON().depth, 2)
    assert.ok(result.legalMoves.includes(result.search.move))
    assert.deepEqual(result.nextState.moves, ['e2e4', result.search.move])
    assert.equal(result.nextState.turn, 'white')
    assert.ok(result.search.nodes <= 50_000 && result.search.depth <= 2)
    await waitForPosition(page, result.nextState.fen)
    assert.ok(await page.getByRole('button', { name: 'e4, White pawn', exact: true }).isVisible())
    assert.equal(searchRequests.length, 1)
    await assertExport(page, python, result.nextState)
    await screenshot(page, 'qchess-desktop.png')
  })

  await t.test('undo removes one ply without automatically replaying it', async () => {
    const count = searchRequests.length
    const { result } = await act(page, '/api/state', () => page.getByRole('button', { name: 'Undo move', exact: true }).click())
    assert.deepEqual(result.moves, ['e2e4'])
    assert.equal(result.turn, 'black')
    await page.waitForLoadState('networkidle')
    assert.equal(searchRequests.length, count)
    await assertExport(page, python, result)
    const reply = (await act(page, '/api/search', () => page.getByRole('button', { name: 'Engine move', exact: true }).click())).result
    assert.ok(reply.legalMoves.includes(reply.search.move))
    assert.equal(reply.nextState.moves.length, 2)
    assert.equal(searchRequests.length, count + 1)
  })

  await t.test('castling and en passant update the actual board and history', async () => {
    await newGame(page)
    const loaded = await loadPosition(page, CASTLING)
    assert.deepEqual(loaded.moves, [])
    const castled = await move(page, 'e1', 'g1')
    assert.deepEqual(castled.moves, ['e1g1'])
    for (const label of ['g1, White king', 'f1, White rook', 'h1, empty', 'e1, empty']) {
      assert.ok(await page.getByRole('button', { name: label, exact: true }).isVisible())
    }
    await assertExport(page, python, castled)
    await newGame(page)
    let captured
    for (const [from, to] of [['e2', 'e4'], ['a7', 'a6'], ['e4', 'e5'], ['d7', 'd5'], ['e5', 'd6']]) {
      captured = await move(page, from, to)
    }
    assert.deepEqual(captured.moves, ['e2e4', 'a7a6', 'e4e5', 'd7d5', 'e5d6'])
    for (const label of ['d6, White pawn', 'd5, empty', 'e5, empty']) {
      assert.ok(await page.getByRole('button', { name: label, exact: true }).isVisible())
    }
    await assertExport(page, python, captured)
  })

  await t.test('promotion can be canceled or committed to the selected piece', async () => {
    await loadPosition(page, PROMOTION)
    await square(page, 'a7').click()
    await square(page, 'a8').click()
    const dialog = page.getByRole('dialog', { name: 'Choose promotion', exact: true })
    await dialog.waitFor({ state: 'visible' })
    assert.equal(await page.getByLabel('Current FEN', { exact: true }).inputValue(), PROMOTION)
    await page.getByRole('button', { name: 'Cancel promotion', exact: true }).click()
    await dialog.waitFor({ state: 'hidden' })
    assert.equal(await page.getByLabel('Current FEN', { exact: true }).inputValue(), PROMOTION)
    await square(page, 'a7').click()
    await square(page, 'a8').click()
    const { result } = await act(page, '/api/state', () => page.getByRole('button', { name: 'Promote to knight', exact: true }).click())
    assert.deepEqual(result.moves, ['a7a8n'])
    assert.equal(result.outcome.termination, 'insufficient-material')
    assert.ok(await page.getByRole('button', { name: 'a8, White knight', exact: true }).isVisible())
    assert.ok(await page.getByRole('heading', { name: 'Game drawn', exact: true }).isVisible())
    assert.ok(await page.getByRole('button', { name: 'Engine move', exact: true }).isDisabled())
    await assertExport(page, python, result)
  })

  await t.test('an announced draw claim records its move without playing it', async () => {
    const loaded = await loadPosition(page, FIFTY)
    assert.ok(loaded.drawClaims.some(claim => claim.reason === 'fiftyMoves' && claim.intendedMove === 'b2b3'))
    await page.getByLabel('Draw claim', { exact: true }).selectOption({ label: 'Fifty-move rule · announce Rb3' })
    const { result, request } = await act(page, '/api/claim', () => page.getByRole('button', { name: 'Claim draw', exact: true }).click())
    assert.deepEqual(request, { initialFen: FIFTY, moves: [], reason: 'fiftyMoves', intendedMove: 'b2b3' })
    assert.equal(result.fen, FIFTY)
    assert.deepEqual(result.moves, [])
    assert.equal(result.outcome.result, '1/2-1/2')
    for (const label of ['b2, White rook', 'b3, empty']) {
      assert.ok(await page.getByRole('button', { name: label, exact: true }).isVisible())
    }
    const exported = await assertExport(page, python, result)
    assert.match(exported.comment, /Rb3/)
    assert.equal(exported.moves.length, 0)
  })

  for (const action of ['New game', 'Undo move', 'Load position', 'Depth']) {
    await t.test(`${action} discards an earlier completed engine response`, async () => {
      await newGame(page)
      const before = await move(page, 'e2', 'e4')
      await page.getByLabel('Depth', { exact: true }).selectOption('1')
      const expected = await holdSearch(page, origin, async () => {
        if (action === 'New game') return newGame(page, false)
        if (action === 'Undo move') {
          return (await act(page, '/api/state', () => page.getByRole('button', { name: action, exact: true }).click())).result
        }
        if (action === 'Load position') return loadPosition(page, CASTLING)
        await page.getByLabel('Depth', { exact: true }).selectOption('2')
        return before
      })
      await assertExport(page, python, expected)
    })
  }

  await t.test('a completed FEN load preserves a newer unsubmitted editor draft', async () => {
    await newGame(page)
    const draft = PROMOTION
    const { expected } = await holdResponse(page, origin, '/api/state',
      async () => {
        await page.getByLabel('Load FEN', { exact: true }).fill(CASTLING)
        await page.getByRole('button', { name: 'Load position', exact: true }).click()
      },
      async result => {
        assert.equal(result.fen, CASTLING)
        await page.getByLabel('Load FEN', { exact: true }).fill(draft)
        return result
      },
    )
    await waitForPosition(page, CASTLING)
    assert.equal(await page.getByLabel('Load FEN', { exact: true }).inputValue(), draft)
    await assertExport(page, python, expected)
  })

  await t.test('keyboard navigation selects and plays a legal move', async () => {
    await newGame(page)
    await square(page, 'e2').focus()
    await page.keyboard.press('ArrowRight')
    assert.ok(await square(page, 'f2').evaluate(button => button === globalThis.document.activeElement))
    await page.keyboard.press('ArrowLeft')
    await page.keyboard.press('Enter')
    assert.equal(await square(page, 'e2').getAttribute('aria-pressed'), 'true')
    await page.keyboard.press('ArrowUp')
    await page.keyboard.press('ArrowUp')
    assert.ok(await square(page, 'e4').evaluate(button => button === globalThis.document.activeElement))
    const { result } = await act(page, '/api/state', () => page.keyboard.press('Space'))
    assert.deepEqual(result.moves, ['e2e4'])
    const board = page.getByRole('grid', { name: 'Chessboard, White at bottom', exact: true })
    assert.equal(await board.getByRole('button').evaluateAll(buttons => buttons.filter(button => button.tabIndex === 0).length), 1)
  })

  await t.test('a fresh mobile page plays chess without horizontal overflow', async () => {
    const mobileContext = await createContext({ viewport: { width: 320, height: 844 }, isMobile: true, hasTouch: true })
    const mobile = await mobileContext.newPage()
    await mobile.goto(origin)
    await waitForPosition(mobile, STARTING_FEN)
    await mobile.getByLabel('Automatic Black reply', { exact: true }).uncheck()
    const played = await move(mobile, 'e2', 'e4')
    assert.deepEqual(played.moves, ['e2e4'])
    const widths = await mobile.evaluate(() => ({
      document: globalThis.document.documentElement.scrollWidth,
      body: globalThis.document.body.scrollWidth,
    }))
    assert.ok(widths.document <= 321 && widths.body <= 321, `Mobile page overflow: ${JSON.stringify(widths)}`)
    await assertExport(mobile, python, played)
    await screenshot(mobile, 'qchess-mobile.png')
    await loadPosition(mobile, PROMOTION)
    await square(mobile, 'a7').click()
    await square(mobile, 'a8').click()
    const dialog = mobile.getByRole('dialog', { name: 'Choose promotion', exact: true })
    await dialog.waitFor({ state: 'visible' })
    const bounds = await dialog.boundingBox()
    assert.ok(bounds.x >= 0 && bounds.x + bounds.width <= 321, 'Promotion choices must fit the mobile viewport')
    await screenshot(mobile, 'qchess-mobile-promotion.png')
    await mobile.getByRole('button', { name: 'Cancel promotion', exact: true }).click()
    await mobileContext.close()
  })

  await t.test('browser behavior has no security policy failures or third-party traffic', async () => {
    await page.waitForLoadState('networkidle')
    assert.deepEqual(
      { pageErrors, consoleErrors, policyViolations, thirdPartyRequests, dialogs },
      { pageErrors: [], consoleErrors: [], policyViolations: [], thirdPartyRequests: [], dialogs: [] },
    )
  })
})
