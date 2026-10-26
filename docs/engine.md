# Engine design and application contract

Board operations and search live in `src/engine.py`, scoring in
`src/evaluation.py`, and the UCI transport in `src/uci.py`.

## Evaluation

For a nonterminal board, material score is

\[
E(B) = \sum_{p \in \{P,N,B,R,Q\}} v_p\,[n_p(\text{White})-n_p(\text{Black})],
\]

with values 100, 320, 330, 500, and 900 centipawns respectively. A king has no
material bonus because legal play never captures a king. An automatic draw
scores zero. Checkmate uses a magnitude of 100,000, reduced by the search ply
distance, so winning searches prefer shorter mates and losing searches delay
them. These decisive scores are not ordinary pawn advantages.

At a White node the engine maximizes the child score; at a Black node it minimizes
it. Alpha and beta bound the range that can still affect an ancestor's choice.
Captures and promotions are visited first, with UCI notation breaking ties
deterministically. Iterative deepening publishes only a completed iteration.
Cancellation, time, and node checks can interrupt the next iteration without
discarding the previous result. If the first iteration cannot complete, a legal
fallback is returned with depth zero and the current position's evaluation.

The Python and browser APIs report scores from White's perspective. UCI converts
them to the side-to-move perspective required by its protocol. Browser requests
specify the maximum depth; search metadata reports the completed depth and visited
nodes, which can include work in an interrupted final iteration.

## History and rules

An application request supplies a six-field initial FEN and the full sequence of
played UCI moves. The controller reconstructs an independent board, validates each
move, and records SAN before pushing it. Search copies the complete move stack.
This retains the repetition information that an evolving FEN alone would lose.
Earlier history cannot be recovered when the user loads a standalone FEN.

Automatic outcomes are checkmate, stalemate, insufficient material as recognized
by python-chess, seventy-five moves, and fivefold repetition. The underlying
library checks material-based insufficiency; it does not identify every possible
dead position. The workbench does not provide tournament clocks or arbitration.

Threefold repetition and fifty moves are optional claims. A claim can be immediate
or based on one specifically announced legal move. The response lists eligible
`drawClaims`, each with a reason and either a null `intendedMove` for an immediate
claim or that precise UCI move and its SAN. A prospective claim is tested on a
full-history copy. The announced move is not added to the played moves or PGN
movetext. The exported PGN records the draw result and a fixed claim explanation.
Undo or loading a position returns to analysis; a claim is not stored globally.

The distinctions between current and prospective claims, automatic outcomes,
and legal move validation follow the
[python-chess core API](https://python-chess.readthedocs.io/en/latest/core.html).

## Browser endpoints

All endpoints require an exact local Origin and the current session token.
Unknown or missing JSON fields are rejected.

| POST route | Exact fields | Result |
| --- | --- | --- |
| `/api/state` | `initialFen`, `moves` | Validated position and history |
| `/api/search` | `initialFen`, `moves`, `depth` | Supplied position, search metadata, and separate `nextState` |
| `/api/claim` | `initialFen`, `moves`, `reason`, `intendedMove` | Validated claimed result with unchanged played history |

A state includes canonical FEN, pieces, side to move, legal moves, SAN and UCI
history, evaluation, outcome, claim options, PGN, and application-limit flags.
An accepted search returns the position it searched and a separate state after
its verified legal move. The caller must explicitly accept the new state. A
terminal search returns no next state. No request changes a shared server game.

Limits are 160 printable ASCII characters for FEN, 512 played plies, integer FEN
counters no larger than 2,147,483,647, and search depth 1 through 4. Boolean values
do not count as integers. A fullmove number starts at 1. The engine receives fixed
limits of 50,000 nodes and two seconds for a browser search. The time allowance
is cooperative and does not include all validation, notation, and transport work.

At the history or counter limit, `canPlay` is false. The board and PGN remain
available, but further moves and search are unavailable. These limits are
application constraints and do not assign a chess result. History is never
silently truncated to fit them.
