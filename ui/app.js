'use strict';

(() => {
  const STARTING_FEN = 'rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1'
  const FILES = 'abcdefgh'
  const PIECES = {
    p: { name: 'pawn', glyph: '♟' }, n: { name: 'knight', glyph: '♞' },
    b: { name: 'bishop', glyph: '♝' }, r: { name: 'rook', glyph: '♜' },
    q: { name: 'queen', glyph: '♛' }, k: { name: 'king', glyph: '♚' },
  }
  const UCI = /^[a-h][1-8][a-h][1-8][qrbn]?$/
  const SQUARE = /^[a-h][1-8]$/
  const TERMINATIONS = {
    checkmate: 'Checkmate', stalemate: 'Stalemate',
    'insufficient-material': 'Insufficient material',
    'seventyfive-moves': 'Seventy-five-move rule',
    'fivefold-repetition': 'Fivefold repetition',
    'claimed-threefold-repetition': 'Threefold repetition claimed',
    'claimed-fifty-moves': 'Fifty-move rule claimed',
  }
  const byId = id => document.getElementById(id)
  const board = byId('board')
  const status = byId('game-status')
  const errorBox = byId('error')
  const autoReply = byId('auto-reply')
  const depthInput = byId('depth')
  const fenInput = byId('fen-input')
  const promotionDialog = byId('promotion-dialog')
  const squareButtons = new Map()
  const token = document.querySelector('meta[name="csrf-token"]')?.content ?? ''
  const hasToken = /^[a-f0-9]{64}$/.test(token)
  let state = null
  let generation = 0
  let activeController = null
  let busy = null
  let selectedSquare = null
  let focusedSquare = 'e2'
  let pendingPromotion = null
  let lastSearch = null

  class DisplayError extends Error {}

  function unexpected() {
    throw new DisplayError('The local server returned an unexpected position. Reload the page and try again.')
  }

  function isObject(value) {
    return value !== null && typeof value === 'object' && !Array.isArray(value)
  }

  function isText(value, limit) {
    return typeof value === 'string' && value.length <= limit
  }

  function integer(value, minimum, maximum) {
    return Number.isInteger(value) && value >= minimum && value <= maximum
  }

  function isMove(value) {
    return typeof value === 'string' && UCI.test(value)
  }

  function parseClaim(value) {
    if (!isObject(value) || !['threefold', 'fiftyMoves'].includes(value.reason)) unexpected()
    if (value.intendedMove === null) {
      if (value.san !== null) unexpected()
    } else if (!isMove(value.intendedMove) || !isText(value.san, 32) || !value.san) unexpected()
    return { reason: value.reason, intendedMove: value.intendedMove, san: value.san }
  }

  function parseState(value) {
    if (!isObject(value)) unexpected()
    if (!isText(value.initialFen, 160) || !value.initialFen || !isText(value.fen, 160) || !value.fen) unexpected()
    if (!Array.isArray(value.moves) || value.moves.length > 512 || !value.moves.every(isMove)) unexpected()
    if (!Array.isArray(value.san) || value.san.length !== value.moves.length || !value.san.every(move => isText(move, 32) && move)) unexpected()
    if (!['white', 'black'].includes(value.turn) || typeof value.check !== 'boolean') unexpected()
    if (!Array.isArray(value.pieces) || value.pieces.length > 64) unexpected()
    const occupied = new Set()
    const pieces = value.pieces.map(piece => {
      if (!isObject(piece) || typeof piece.square !== 'string' || !SQUARE.test(piece.square)) unexpected()
      if (occupied.has(piece.square) || typeof piece.symbol !== 'string' || !/^[prnbqkPRNBQK]$/.test(piece.symbol)) unexpected()
      if (!['white', 'black'].includes(piece.color) || (piece.symbol === piece.symbol.toUpperCase()) !== (piece.color === 'white')) unexpected()
      occupied.add(piece.square)
      return { square: piece.square, symbol: piece.symbol, color: piece.color }
    })
    if (!Array.isArray(value.legalMoves) || value.legalMoves.length > 256 || !value.legalMoves.every(isMove)) unexpected()
    if (!['playing', 'check', ...Object.keys(TERMINATIONS)].includes(value.status)) unexpected()
    let outcome = null
    if (value.outcome !== null) {
      if (!isObject(value.outcome) || !Object.hasOwn(TERMINATIONS, value.outcome.termination)) unexpected()
      if (![null, 'white', 'black'].includes(value.outcome.winner)) unexpected()
      const expectedResult = value.outcome.winner === 'white' ? '1-0' : value.outcome.winner === 'black' ? '0-1' : '1/2-1/2'
      if (value.outcome.result !== expectedResult) unexpected()
      outcome = { result: value.outcome.result, winner: value.outcome.winner, termination: value.outcome.termination }
    }
    if (!Array.isArray(value.drawClaims) || value.drawClaims.length > 512) unexpected()
    const drawClaims = value.drawClaims.map(parseClaim)
    const claim = value.claim === null ? null : parseClaim(value.claim)
    const evaluation = value.evaluation
    if (!isObject(evaluation) || !integer(evaluation.score, -100000, 100000) || evaluation.perspective !== 'white') unexpected()
    if (!['material', 'mate', 'draw'].includes(evaluation.type)) unexpected()
    if (!isText(value.pgn, 100000)) unexpected()
    for (const field of ['canPlay', 'historyLimitReached', 'counterLimitReached']) {
      if (typeof value[field] !== 'boolean') unexpected()
    }
    if (value.canPlay && (outcome !== null || value.historyLimitReached || value.counterLimitReached)) unexpected()
    return {
      initialFen: value.initialFen, fen: value.fen,
      moves: value.moves.slice(), san: value.san.slice(), turn: value.turn, pieces,
      legalMoves: value.legalMoves.slice(), check: value.check, status: value.status,
      outcome, drawClaims, claim,
      evaluation: { score: evaluation.score, perspective: 'white', type: evaluation.type },
      pgn: value.pgn, canPlay: value.canPlay,
      historyLimitReached: value.historyLimitReached, counterLimitReached: value.counterLimitReached,
    }
  }

  function sameMoves(first, second) {
    return first.length === second.length && first.every((move, index) => move === second[index])
  }

  function parseSearch(value, requested) {
    const before = parseState(value)
    if (before.initialFen !== requested.initialFen || !sameMoves(before.moves, requested.moves)) unexpected()
    const result = value.search
    if (!isObject(result) || !integer(result.score, -100000, 100000) || !integer(result.depth, 0, 4) || !integer(result.nodes, 0, 50000)) unexpected()
    if (result.move === null) {
      if (result.san !== null || value.nextState !== null) unexpected()
    } else if (!isMove(result.move) || !before.legalMoves.includes(result.move) || !isText(result.san, 32) || !result.san) unexpected()
    const nextState = value.nextState === null ? null : parseState(value.nextState)
    if (result.move !== null && (nextState === null || nextState.initialFen !== before.initialFen || !sameMoves(nextState.moves, [...before.moves, result.move]))) unexpected()
    return {
      before, nextState,
      search: { move: result.move, san: result.san, score: result.score, depth: result.depth, nodes: result.nodes },
    }
  }

  function titleCase(value) {
    return value[0].toUpperCase() + value.slice(1)
  }

  function clearError() {
    errorBox.hidden = true
    errorBox.textContent = ''
    fenInput.removeAttribute('aria-invalid')
  }

  function showError(message) {
    errorBox.textContent = message
    errorBox.hidden = false
  }

  function closePromotion() {
    pendingPromotion = null
    if (promotionDialog.open) promotionDialog.close()
  }

  function humanCanMove() {
    return state !== null && state.canPlay && busy === null && hasToken && (!autoReply.checked || state.turn === 'white')
  }

  function positionDescription(position) {
    if (position.outcome !== null) {
      const result = position.outcome.winner === null ? 'Draw' : `${titleCase(position.outcome.winner)} wins`
      return `${result}. ${TERMINATIONS[position.outcome.termination]}.`
    }
    if (position.historyLimitReached) return 'The 512 half-move limit is reached. Undo a move or start a new game.'
    if (position.counterLimitReached) return 'The supported FEN counter limit is reached. Undo a move or load another position.'
    return `${titleCase(position.turn)} to move${position.check ? ', in check' : ''}.`
  }

  function renderControls() {
    const playable = state !== null && state.canPlay
    byId('engine-move').disabled = !hasToken || !playable || busy !== null
    byId('undo-move').disabled = !hasToken || state === null || state.moves.length === 0
    byId('new-game').disabled = !hasToken
    byId('load-position').disabled = !hasToken
    byId('download-pgn').disabled = state === null || busy !== null
    const claimable = state !== null && state.outcome === null && state.drawClaims.length > 0
    byId('draw-choice').disabled = !hasToken || !claimable || busy !== null
    byId('claim-draw').disabled = !hasToken || !claimable || busy !== null
    board.setAttribute('aria-busy', String(busy !== null))
    byId('white-role').textContent = autoReply.checked ? 'You' : 'Manual'
    byId('black-role').textContent = autoReply.checked ? 'Engine' : 'Manual'
    byId('mode-help').textContent = autoReply.checked
      ? 'Play White; Black replies after your move. Turn this off to play either side.'
      : 'Move either side manually, or select Engine move to play the current side.'
    byId('white-turn').hidden = state === null || state.turn !== 'white' || !state.canPlay
    byId('black-turn').hidden = state === null || state.turn !== 'black' || !state.canPlay
    let badge = state === null ? 'Connecting' : state.outcome !== null ? state.outcome.result : state.canPlay ? state.check ? 'Check' : 'In play' : 'Limit reached'
    if (busy !== null) badge = busy === 'search' ? 'Searching' : 'Validating'
    byId('position-badge').textContent = badge
  }

  function createBoard() {
    for (let rank = 8; rank >= 1; rank -= 1) {
      const row = document.createElement('div')
      row.className = 'board-row'
      row.setAttribute('role', 'row')
      for (let file = 0; file < 8; file += 1) {
        const square = `${FILES[file]}${rank}`
        const cell = document.createElement('div')
        cell.className = 'board-cell'
        cell.setAttribute('role', 'gridcell')
        const button = document.createElement('button')
        button.type = 'button'
        button.dataset.square = square
        const piece = document.createElement('span')
        piece.className = 'piece'
        piece.setAttribute('aria-hidden', 'true')
        button.append(piece)
        const destination = document.createElement('span')
        destination.className = 'destination'
        destination.setAttribute('aria-hidden', 'true')
        destination.hidden = true
        button.append(destination)
        if (file === 0) {
          const label = document.createElement('span')
          label.className = 'coordinate rank-coordinate'
          label.setAttribute('aria-hidden', 'true')
          label.textContent = String(rank)
          button.append(label)
        }
        if (rank === 1) {
          const label = document.createElement('span')
          label.className = 'coordinate file-coordinate'
          label.setAttribute('aria-hidden', 'true')
          label.textContent = FILES[file]
          button.append(label)
        }
        squareButtons.set(square, { button, piece, destination, light: (file + 8 - rank) % 2 === 0 })
        cell.append(button)
        row.append(cell)
      }
      board.append(row)
    }
  }

  function renderBoard() {
    const pieces = new Map((state?.pieces ?? []).map(piece => [piece.square, piece]))
    const destinations = new Set(selectedSquare === null ? [] : (state?.legalMoves ?? []).filter(move => move.startsWith(selectedSquare)).map(move => move.slice(2, 4)))
    const latest = state?.moves.at(-1) ?? ''
    for (const [square, elements] of squareButtons) {
      const piece = pieces.get(square)
      const destination = destinations.has(square)
      const inCheck = piece !== undefined && state?.check && piece.color === state.turn && piece.symbol.toLowerCase() === 'k'
      const classes = ['square', elements.light ? 'light' : 'dark']
      if (latest.slice(0, 2) === square || latest.slice(2, 4) === square) classes.push('last-move')
      if (selectedSquare === square) classes.push('selected')
      if (destination && piece) classes.push('capture-target')
      if (inCheck) classes.push('in-check')
      elements.button.className = classes.join(' ')
      const description = piece === undefined ? 'empty' : `${titleCase(piece.color)} ${PIECES[piece.symbol.toLowerCase()].name}`
      elements.button.setAttribute('aria-label', `${square}, ${description}${destination ? ', legal destination' : ''}${inCheck ? ', in check' : ''}`)
      elements.button.setAttribute('aria-pressed', String(selectedSquare === square))
      elements.button.setAttribute('aria-disabled', String(!humanCanMove()))
      elements.button.tabIndex = square === focusedSquare ? 0 : -1
      elements.piece.className = piece === undefined ? 'piece' : `piece ${piece.color}`
      elements.piece.textContent = piece === undefined ? '' : PIECES[piece.symbol.toLowerCase()].glyph
      elements.destination.hidden = !destination
    }
  }

  function renderHistory() {
    const body = byId('history-body')
    body.replaceChildren()
    const played = state?.san ?? []
    byId('empty-history').hidden = played.length > 0
    byId('move-count').textContent = `${played.length} half-move${played.length === 1 ? '' : 's'}`
    if (state === null) return
    const fields = state.initialFen.split(' ')
    let turn = fields[1]
    let number = Number(fields[5])
    const rows = []
    for (let index = 0; index < played.length; index += 1) {
      if (turn === 'w' || rows.length === 0) rows.push({ number, white: null, black: null })
      const row = rows.at(-1)
      row[turn === 'w' ? 'white' : 'black'] = { san: played[index], latest: index === played.length - 1 }
      if (turn === 'b') number += 1
      turn = turn === 'w' ? 'b' : 'w'
    }
    const fragment = document.createDocumentFragment()
    for (const item of rows) {
      const row = document.createElement('tr')
      const numberCell = document.createElement('th')
      numberCell.scope = 'row'
      numberCell.textContent = `${item.number}.`
      row.append(numberCell)
      for (const side of ['white', 'black']) {
        const cell = document.createElement('td')
        cell.textContent = item[side]?.san ?? '—'
        if (item[side]?.latest) cell.className = 'latest-move'
        row.append(cell)
      }
      fragment.append(row)
    }
    body.append(fragment)
    const region = byId('history-scroll')
    region.scrollTop = region.scrollHeight
  }

  function drawReason(reason) {
    return reason === 'threefold' ? 'Threefold repetition' : 'Fifty-move rule'
  }

  function renderDrawNote() {
    const index = Number(byId('draw-choice').value)
    const choice = state?.drawClaims[index]
    byId('draw-note').textContent = choice === undefined ? '' : choice.intendedMove === null
      ? 'Claim the draw in the current position.'
      : `Announce ${choice.san}; this move will be recorded with the claim and will not be played.`
  }

  function renderDrawClaims() {
    const choices = byId('draw-choice')
    choices.replaceChildren()
    const offered = state !== null && state.outcome === null ? state.drawClaims : []
    offered.forEach((claim, index) => {
      const option = document.createElement('option')
      option.value = String(index)
      option.textContent = `${drawReason(claim.reason)} · ${claim.intendedMove === null ? 'current position' : `announce ${claim.san}`}`
      choices.append(option)
    })
    byId('draw-panel').hidden = offered.length === 0
    renderDrawNote()
  }

  function pawnScore(score) {
    return `${score > 0 ? '+' : ''}${(score / 100).toFixed(2)}`
  }

  function renderEvaluation() {
    const output = byId('evaluation')
    if (state === null) return
    const terminal = state.evaluation.type !== 'material'
    output.classList.toggle('terminal-evaluation', terminal)
    if (state.evaluation.type === 'draw') output.textContent = 'Draw'
    else if (state.evaluation.type === 'mate') output.textContent = `${state.evaluation.score > 0 ? 'White' : 'Black'} wins`
    else output.textContent = pawnScore(state.evaluation.score)
    byId('evaluation-note').textContent = terminal ? 'Terminal game result.' : 'Material in pawns · positive favors White.'
  }

  function renderSearch() {
    byId('last-search').hidden = lastSearch === null
    if (lastSearch === null) return
    const result = lastSearch.search
    byId('search-move').textContent = result.move === null ? 'No move available' : `${titleCase(lastSearch.turn)} played ${result.san}`
    if (Math.abs(result.score) >= 99996) {
      const moves = Math.ceil((100000 - Math.abs(result.score)) / 2)
      byId('search-score').textContent = `${result.score > 0 ? 'White' : 'Black'} mates${moves > 0 ? ` in ${moves}` : ''}`
    } else byId('search-score').textContent = `${pawnScore(result.score)} pawns`
    byId('search-work').textContent = `${result.depth} ${result.depth === 1 ? 'ply' : 'plies'} · ${result.nodes.toLocaleString('en-US')} nodes`
    byId('search-note').textContent = result.depth === 0
      ? 'A legal fallback was used before any search depth completed. Its score describes the position before that move.'
      : 'Score from the last completed search depth, viewed from White’s side.'
  }

  function applyState(next, message, search = null) {
    closePromotion()
    selectedSquare = null
    state = next
    lastSearch = search
    clearError()
    byId('current-fen').value = state.fen
    byId('position-heading').textContent = state.outcome !== null
      ? state.outcome.winner === null ? 'Game drawn' : `${titleCase(state.outcome.winner)} wins`
      : !state.canPlay ? 'Play paused at limit' : `${titleCase(state.turn)} to move${state.check ? ' · check' : ''}`
    renderControls()
    renderBoard()
    renderHistory()
    renderDrawClaims()
    renderEvaluation()
    renderSearch()
    status.textContent = message ?? positionDescription(state)
  }

  function cancelOperation(message) {
    generation += 1
    activeController?.abort()
    activeController = null
    busy = null
    closePromotion()
    selectedSquare = null
    clearError()
    renderControls()
    renderBoard()
    if (message) status.textContent = message
  }

  async function requestJSON(kind, endpoint, payload, parse, message) {
    cancelOperation()
    const requestGeneration = generation
    const controller = new AbortController()
    activeController = controller
    busy = kind
    let timedOut = false
    const timeout = setTimeout(() => {
      timedOut = true
      controller.abort()
    }, 10000)
    status.textContent = message
    renderControls()
    renderBoard()
    try {
      const response = await fetch(endpoint, {
        method: 'POST', credentials: 'same-origin', cache: 'no-store', redirect: 'error',
        headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': token },
        body: JSON.stringify(payload), signal: controller.signal,
      })
      if (requestGeneration !== generation) return null
      if (response.status === 403) throw new DisplayError('Reload this page to refresh the local session, then try again.')
      if (response.status === 429) throw new DisplayError('The local server is busy. Wait a moment, then select the action again. No move was applied.')
      const body = await response.json()
      if (requestGeneration !== generation) return null
      if (!response.ok) {
        const detail = typeof body?.error === 'string' && body.error.length <= 160 ? body.error : 'The local server could not validate this action.'
        if (kind === 'load' && response.status === 400) fenInput.setAttribute('aria-invalid', 'true')
        throw new DisplayError(detail)
      }
      const data = parse(body)
      if (requestGeneration !== generation) return null
      return { data, generation: requestGeneration }
    } catch (error) {
      if (requestGeneration !== generation) return null
      showError(timedOut
        ? 'The request timed out. Check that the local Python process is running, then try the action again.'
        : error instanceof DisplayError ? error.message : 'The local Python process could not be reached. Check that it is running, then try again.')
      status.textContent = state === null ? 'No position has been loaded. Select New game to retry.' : `The board is unchanged. ${positionDescription(state)}`
      return null
    } finally {
      clearTimeout(timeout)
      if (requestGeneration === generation) {
        activeController = null
        busy = null
        renderControls()
        renderBoard()
      }
    }
  }

  function currentHistory() {
    return { initialFen: state.initialFen, moves: state.moves.slice() }
  }

  async function loadPosition(initialFen, kind = 'load') {
    if (!hasToken) return
    const reply = await requestJSON(kind, '/api/state', { initialFen, moves: [] }, parseState, 'Validating the position…')
    if (reply === null || reply.generation !== generation) return
    applyState(reply.data, `Position loaded. ${positionDescription(reply.data)}`)
    if (kind === 'load' && fenInput.value === initialFen) fenInput.value = reply.data.initialFen
  }

  async function playMove(move) {
    if (!humanCanMove() || !state.legalMoves.includes(move)) return
    const movingSide = state.turn
    const payload = currentHistory()
    payload.moves.push(move)
    const reply = await requestJSON('move', '/api/state', payload, parseState, 'Validating your move…')
    if (reply === null || reply.generation !== generation) return
    const notation = reply.data.san.at(-1)
    applyState(reply.data, `${notation} played. ${positionDescription(reply.data)}`)
    if (movingSide === 'white' && autoReply.checked && state.canPlay && state.turn === 'black') void playEngineMove()
  }

  async function playEngineMove() {
    if (!hasToken || state === null || !state.canPlay || busy !== null) return
    const depth = Number(depthInput.value)
    if (!integer(depth, 1, 4)) {
      showError('Choose an engine depth from 1 to 4.')
      return
    }
    const payload = { ...currentHistory(), depth }
    const reply = await requestJSON('search', '/api/search', payload, value => parseSearch(value, payload), `${titleCase(state.turn)} is searching at up to ${depth} ${depth === 1 ? 'ply' : 'plies'}…`)
    if (reply === null || reply.generation !== generation) return
    const result = reply.data
    const next = result.nextState ?? result.before
    const message = result.search.move === null ? positionDescription(next) : `${result.search.san} played by the engine. ${positionDescription(next)}`
    applyState(next, message, { turn: result.before.turn, search: result.search })
  }

  async function undoMove() {
    if (!hasToken || state === null || state.moves.length === 0) return
    const payload = currentHistory()
    payload.moves.pop()
    const reply = await requestJSON('undo', '/api/state', payload, parseState, 'Restoring the previous position…')
    if (reply === null || reply.generation !== generation) return
    const note = autoReply.checked && reply.data.canPlay && reply.data.turn === 'black' ? ' Select Engine move to resume Black.' : ''
    applyState(reply.data, `One move undone. ${positionDescription(reply.data)}${note}`)
  }

  async function claimDraw() {
    if (!hasToken || busy !== null || state === null || state.outcome !== null) return
    const choice = state.drawClaims[Number(byId('draw-choice').value)]
    if (choice === undefined) return
    const payload = { ...currentHistory(), reason: choice.reason, intendedMove: choice.intendedMove }
    const reply = await requestJSON('claim', '/api/claim', payload, value => {
      const result = parseState(value)
      if (result.initialFen !== payload.initialFen || !sameMoves(result.moves, payload.moves) || result.claim === null) unexpected()
      if (result.claim.reason !== choice.reason || result.claim.intendedMove !== choice.intendedMove) unexpected()
      return result
    }, 'Validating the draw claim…')
    if (reply === null || reply.generation !== generation) return
    const announcement = choice.intendedMove === null ? '' : ` ${choice.san} was announced and was not played.`
    applyState(reply.data, `${positionDescription(reply.data)}${announcement}`)
  }

  function showPromotion(moves) {
    pendingPromotion = { moves: moves.slice(), generation }
    for (const button of promotionDialog.querySelectorAll('[data-promotion]')) button.disabled = !moves.some(move => move.endsWith(button.dataset.promotion))
    byId('promotion-description').textContent = `Promote the pawn moving from ${moves[0].slice(0, 2)} to ${moves[0].slice(2, 4)}.`
    promotionDialog.showModal()
  }

  function selectSquare(square) {
    focusedSquare = square
    if (!humanCanMove()) {
      if (state !== null && busy === null) status.textContent = !state.canPlay ? positionDescription(state) : 'Black is controlled by the engine. Select Engine move, or turn off Automatic Black reply to move manually.'
      renderBoard()
      return
    }
    if (selectedSquare === square) {
      selectedSquare = null
      status.textContent = positionDescription(state)
      renderBoard()
      return
    }
    if (selectedSquare !== null) {
      const moves = state.legalMoves.filter(move => move.slice(0, 2) === selectedSquare && move.slice(2, 4) === square)
      if (moves.length > 0) {
        if (moves.some(move => move.length === 5)) showPromotion(moves)
        else void playMove(moves[0])
        return
      }
    }
    const piece = state.pieces.find(candidate => candidate.square === square)
    if (piece !== undefined && piece.color === state.turn) {
      const moves = state.legalMoves.filter(move => move.startsWith(square))
      selectedSquare = moves.length === 0 ? null : square
      status.textContent = moves.length === 0
        ? `The ${PIECES[piece.symbol.toLowerCase()].name} on ${square} has no legal move.`
        : `${titleCase(piece.color)} ${PIECES[piece.symbol.toLowerCase()].name} selected on ${square}. Choose a highlighted destination.`
    } else status.textContent = selectedSquare === null ? `Select a ${state.turn} piece to move.` : 'Choose a highlighted destination, or select another piece.'
    renderBoard()
  }

  board.addEventListener('click', event => {
    const button = event.target.closest('button[data-square]')
    if (button !== null && board.contains(button)) selectSquare(button.dataset.square)
  })
  board.addEventListener('keydown', event => {
    const button = event.target.closest('button[data-square]')
    if (button === null || !board.contains(button)) return
    const square = button.dataset.square
    let file = FILES.indexOf(square[0])
    let rank = Number(square[1])
    if (event.key === 'ArrowLeft') file = Math.max(0, file - 1)
    else if (event.key === 'ArrowRight') file = Math.min(7, file + 1)
    else if (event.key === 'ArrowUp') rank = Math.min(8, rank + 1)
    else if (event.key === 'ArrowDown') rank = Math.max(1, rank - 1)
    else if (event.key === 'Home') { file = 0; if (event.ctrlKey) rank = 8 }
    else if (event.key === 'End') { file = 7; if (event.ctrlKey) rank = 1 }
    else if (event.key === 'Escape') {
      selectedSquare = null
      renderBoard()
      return
    } else return
    event.preventDefault()
    focusedSquare = `${FILES[file]}${rank}`
    renderBoard()
    squareButtons.get(focusedSquare).button.focus()
  })
  promotionDialog.addEventListener('click', event => {
    const button = event.target.closest('button[data-promotion]')
    if (button === null || button.disabled || pendingPromotion === null) return
    const selected = pendingPromotion
    const move = selected.moves.find(candidate => candidate.endsWith(button.dataset.promotion))
    closePromotion()
    if (selected.generation === generation && move !== undefined) void playMove(move)
  })
  byId('cancel-promotion').addEventListener('click', () => {
    closePromotion()
    selectedSquare = null
    renderBoard()
    status.textContent = 'Promotion canceled. Select a piece to continue.'
  })
  promotionDialog.addEventListener('cancel', () => {
    pendingPromotion = null
    selectedSquare = null
    renderBoard()
    status.textContent = 'Promotion canceled. Select a piece to continue.'
  })
  byId('engine-move').addEventListener('click', () => { void playEngineMove() })
  byId('undo-move').addEventListener('click', () => { void undoMove() })
  byId('new-game').addEventListener('click', () => {
    autoReply.checked = true
    depthInput.value = '2'
    fenInput.value = ''
    focusedSquare = 'e2'
    void loadPosition(STARTING_FEN, 'new')
  })
  byId('fen-form').addEventListener('submit', event => {
    event.preventDefault()
    void loadPosition(fenInput.value, 'load')
  })
  autoReply.addEventListener('change', () => {
    cancelOperation('Play mode changed. Pending engine results were discarded.')
  })
  depthInput.addEventListener('change', () => {
    cancelOperation('Search depth changed. Select Engine move when ready.')
  })
  byId('draw-choice').addEventListener('change', renderDrawNote)
  byId('claim-draw').addEventListener('click', () => { void claimDraw() })
  byId('download-pgn').addEventListener('click', () => {
    if (state === null || busy !== null) return
    try {
      const url = URL.createObjectURL(new Blob([state.pgn], { type: 'application/x-chess-pgn;charset=utf-8' }))
      const link = document.createElement('a')
      link.href = url
      link.download = 'qchess-game.pgn'
      document.body.append(link)
      link.click()
      link.remove()
      setTimeout(() => URL.revokeObjectURL(url), 1000)
      status.textContent = 'qchess-game.pgn download started.'
    } catch {
      showError('The PGN download could not start. Try again in this browser.')
    }
  })

  createBoard()
  renderControls()
  renderBoard()
  if (hasToken) void loadPosition(STARTING_FEN, 'initial')
  else {
    showError('Open this page through the local QChess server, then reload it.')
    status.textContent = 'A local server session is required to load the board.'
  }
})()
