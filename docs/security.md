# Local application security boundary

The workbench is a single-user application for a trusted computer. Its Python
process binds only to `127.0.0.1`. The browser sends game state to this local
process; no remote service, downloaded engine executable, or external asset is
used. There is no public-host option or shared server-side game.

## Request admission and resource bounds

Every request must carry the exact local Host. POST requests also require the
exact Origin and a random per-process token in `X-CSRF-Token`. Token comparisons
use constant time. There is no CORS allowance. This boundary addresses unwanted
browser cross-origin requests and DNS rebinding; it does not isolate untrusted
programs already running on the same computer.

The transport serves a fixed list of preloaded assets and application routes.
URLs are never translated to filesystem paths. Request lines, headers, body
size, nesting depth, active connections, read time, total connection time, and
response size are bounded. Duplicate framing, transfer encoding, compression,
malformed JSON, duplicate JSON keys, and non-finite numbers are rejected. Each
connection closes after one request. At most eight connection workers and one
application calculation run at a time; additional calculations receive 429 with
a retry indication instead of joining an unbounded queue.

The chess controller validates exact schemas, bounded FEN and counters, full
legal history, promotion syntax, and search depth. It accepts no Python code,
shell command, file destination, arbitrary engine options, or uploaded PGN.
Work is bounded by the 512-ply history limit and the fixed engine budget. No
request launches a subprocess. Search uses a private full-history board copy,
and one browser's request cannot mutate another browser's game.

## Browser state and downloads

Scripts, styles, and the icon are served locally under a restrictive content
security policy. No inline code or expression evaluation is required. The UI
builds content through DOM text and fixed elements, uses no persistent browser
storage, and sends requests only to the fixed local endpoints.

Each asynchronous action belongs to a state generation. Reset, undo, and position
replacement invalidate earlier work so a delayed result cannot overwrite the
current board or export. Client cancellation does not immediately terminate an
already running Python search; that calculation retains its fixed short budget.
During that interval a new calculation can receive 429 and can be retried after
the current calculation finishes.

The PGN download is an explicit local action with a fixed filename. Movetext and
notation are generated from the validated board; the server accepts no arbitrary
PGN headers or comments. A download does not write to a server path. Reloading the
page discards the browser's current game. No game contents, request tokens, or
exception details are logged by the local HTTP transport.

Responses use no-store caching, no-referrer, anti-framing, content-type protection,
restricted browser permissions, and a same-origin resource policy. Public error
responses use bounded fixed messages instead of exception details.

## Other interfaces and operation

The Python API is a trusted in-process interface. A caller can access its own
`chess.Board`; web input must continue to pass through the bounded controller.
The separate UCI command line validates and limits its input and uses one
cancellable search worker. It exposes no network listener.

Stop the local server with Ctrl+C. Shutdown closes its listener and active
sockets, then joins workers with a bounded wait. Endpoint implementations must
continue to provide bounded computation; Python threads cannot forcibly interrupt
arbitrary future callback code.

The verification suite exercises rules and scoring, real UCI subprocesses,
HTTP authorization and framing, concurrency and deadlines, application routes,
and actual browser state transitions. Dependency audits check the committed
Python and browser-development locks at scan time. They identify known advisories
and do not establish that unknown vulnerabilities are absent.

This loopback transport has not been designed or assessed as a public hosting
service. Public deployment would need its own authentication, TLS, isolation,
rate controls, and operating procedures. Python documents the production
limitations of its standard server components in the
[HTTP server documentation](https://docs.python.org/3.12/library/http.server.html).
