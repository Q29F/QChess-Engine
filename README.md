# QChess Engine

A local chess workbench with a bounded material-based engine, Python API, and
UCI interface.

## Start the browser workbench

Use Python 3.12.16 or a later security release in the 3.12 series, with pip
available in that Python installation. From the repository root:

```bash
python3.12 -m venv --without-pip .venv
python3.12 -m pip --python .venv install --require-hashes --only-binary=:all: -r requirements-installer.txt
source .venv/bin/activate
python -m pip install --require-hashes --no-build-isolation -r requirements.txt
python web.py
```

On Windows, activate with `.venv\Scripts\activate`. Open the
`http://127.0.0.1:<port>` address printed by the program. The port is chosen
automatically; `python web.py --port 8766` selects a fixed port. Stop the process
with Ctrl+C. Node.js is needed only for browser verification, not to run the app.

The workbench supports legal square selection, promotion choices, engine moves,
move history, undo, a new game, FEN position loading, and a PGN download. It starts
with the human playing White and the engine replying as Black. Turn off
**Automatic Black reply** to play both sides. Undo removes one played move and
pauses automatic replies. Position analysis and optional draw claims use the
same validated history as the board.

Each browser session holds its own game. No remote service or account is used.
The local Python process receives only the initial FEN, played moves, and the
requested action. Reloading the page starts a fresh game; download the PGN to keep
the current result. Loading FEN starts a new history because FEN cannot encode
earlier repetitions.

## Rules and engine behavior

- Legal moves, castling, en passant, promotion, check, and automatic endings use
  the pinned [python-chess rules library](https://python-chess.readthedocs.io/en/latest/core.html).
- The workbench controller validates every played move from the initial position. It rejects
  null moves and moves after an automatic ending.
- Threefold repetition and the fifty-move rule require an explicit draw claim.
  When a claim depends on an intended next move, the caller must select that
  particular legal move. The claim records it as announced without playing it.
- Material values are pawn 100, knight 320, bishop 330, rook 500, and queen 900
  centipawns. Positive scores favor White; negative scores favor Black. Mate and
  draw results are identified separately.
- Search uses deterministic move ordering, iterative deepening, and alpha-beta
  pruning on an independent full-history board copy. The workbench allows depth
  1 through 4 and fixes the search budget at 50,000 nodes and two seconds.
  Time checks are cooperative; validation and response construction add overhead.
- At most 512 plies are accepted by the browser API. Reaching that application
  limit stops additional play without declaring a chess result or truncating
  repetition history. Undo or start a new game to continue working.

This is a material-based educational engine. It does not implement positional
evaluation, quiescence search, opening books, tablebases, variants, or a quantum
backend. Playing strength has not been benchmarked. Automatic insufficient
material detection follows python-chess's material rules; it does not prove every
possible dead position. See [engine design](docs/engine.md) for the equations,
history rules, and interface contracts.

## UCI command line

```bash
python -m src.qchess --mode uci
```

Direct execution with `python src/qchess.py --mode uci` also works. A chess GUI can
launch the virtual environment's Python executable with `-m src.qchess --mode uci`
and the repository root as its working directory, or use the absolute script path.
Responses go to standard output and diagnostics to standard error. There is no
separate native build step. The hash-pinned `chess` dependency is distributed as
source and is built with the locked setuptools backend during installation.

Supported [UCI commands](https://backscattering.de/chess/uci/) are `uci`, `isready`,
`ucinewgame`, `position startpos [moves ...]`, `position fen <six fields> [moves ...]`,
`go`, `stop`, and `quit`. `go` accepts `depth`, `nodes`, `movetime`, `wtime`, `btime`,
`winc`, `binc`, `movestogo`, and `infinite`; clock values use milliseconds.

UCI requests are capped at depth 4, 100,000 nodes, and ten seconds of computation.
A plain `go` uses depth 2, 50,000 nodes, and one second. Clock-based requests allocate
a bounded share of the remaining time. Larger valid limits are capped; negative,
malformed, duplicate, and unsupported parameters are rejected.

`go infinite` completes bounded calculation and then waits for `stop` before
emitting `bestmove`. `isready` remains responsive. Replacing a position or search
suppresses obsolete results; `quit` and input EOF stop the worker. `bestmove 0000`
is emitted only if no legal move exists. The GUI decides how to present an
automatic draw that still has legal moves.

Commands are limited to 16,384 ASCII characters and 1,033 tokens, with at most
1,024 moves in a position command. Integer parameters have at most ten digits and
may not exceed 2,147,483,647, including FEN counters after each replayed move.
Invalid position commands leave the current game intact. An oversized physical
line terminates the process with exit code 2.
Unsupported `go` features such as `ponder`, `mate`, and `searchmoves` return an
error. Unknown command names are ignored; no configurable options are advertised.

For a manual session, enter `uci`, `isready`, `position startpos moves e2e4`, and
`go depth 2`. Wait for `bestmove` before entering `quit`.

## Python API

```python
from src.api import ChessEngine, evaluate_position

engine = ChessEngine()
assert len(engine.get_legal_moves()) == 20
engine.make_move("e2e4")
result = engine.search(depth=2, max_nodes=50_000, time_limit=1.0)
print(result.move, result.score)  # UCI move; score from White's perspective
assert engine.undo_move() == "e2e4"
assert evaluate_position(engine.board) == 0
```

`QChessEngine` remains an alias for `ChessEngine`. `get_board_state()` returns
canonical FEN. Invalid mutations raise `TypeError` or `ValueError`. The Python
search API validates limits and accepts an optional `threading.Event` for
cancellation. `SearchResult.depth == 0` identifies a terminal result or an
unsearched legal fallback; automatic endings return `move=None`. Drive each
engine or UCI handler from one caller thread.

## Organization

| Location | Responsibility |
| --- | --- |
| `src/api.py` | Public Python imports |
| `src/engine.py` | Board operations and bounded search |
| `src/evaluation.py` | Material and terminal scoring |
| `src/uci.py`, `src/utils.py`, `src/qchess.py` | UCI protocol and command-line entry |
| `workbench.py` | Stateless validation, state, search, draw claims, and PGN |
| `localServer.py`, `web.py` | Restricted loopback transport and fixed routes |
| `ui/` | Local HTML, CSS, JavaScript, and icon |
| `tests/` | Rules, protocol, transport, application, and browser checks |
| `docs/` | Engine design and security boundary |

Python uses `src` as an implicit namespace package.

## Verification and maintenance

```bash
python -m compileall -q src tests workbench.py web.py localServer.py
python -m unittest discover -s tests -v
npm ci --ignore-scripts
npx playwright install chromium
npm run test:browser
npm audit --audit-level=low
python -m pip install pip-audit==2.10.1
python -m pip_audit -r requirements.txt -r requirements-installer.txt --disable-pip --no-deps
```

The browser checks require Node.js 22.13 or later in the 22 series, or Node.js 24,
and a compatible Chromium installation. CI uses Node.js 24 and Python 3.12.16, with
read-only repository permissions and pinned action revisions. It installs the
browser and required system libraries before testing. Dependabot checks Python,
browser verification dependencies, and GitHub Actions weekly.

To refresh the Python lock, review the upstream release, update `requirements.in`,
then run:

```bash
uv pip compile requirements.in --generate-hashes --no-header --python-version 3.12 --output-file requirements.txt
uv pip compile requirements-installer.in --generate-hashes --no-header --universal --python-version 3.12 --output-file requirements-installer.txt
```

The base Python's pip installs the locked tools into `.venv` without upgrading
itself. The installer lock pins pip and setuptools separately from the chess
runtime dependency. `--no-build-isolation` uses that locked backend without
downloading additional build tools.

See [security boundary](docs/security.md) for the local service controls and their
limits. Dependency audits identify known advisories at scan time.

## License

See the existing [LICENSE](LICENSE) for the repository's licensing terms.
