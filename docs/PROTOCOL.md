# Wire protocol

This describes `relay.py`, `bridge.lua` and the shared Python client. See
[REFERENCE.md](REFERENCE.md) for tool usage and [../SECURITY.md](../SECURITY.md) for deployment.
There is no protocol version field. JSON examples omit optional fields when unnecessary.

## HTTP control API

The default address is `http://127.0.0.1:8765`. `/api/*` requires the exact header
`Authorization: Bearer <api-token>`. Send JSON with `Content-Type: application/json`.
The API token is separate from the computer token; a computer token cannot call the control API.

| Method and path | Authentication | Successful response |
|---|---|---|
| `GET /api/computers` | API token | `{"computers": [...]}` (no `ok` field) |
| `POST /api/call` | API token | The operation result, including failures with `ok: false` |
| `GET /healthz` | None | `{"ok": true, "computers": 1}`; count of connected computers, not a bridge liveness probe |
| `GET /bridge.lua` | None | Packaged script as `text/plain`; no secrets embedded |
| `GET /ws` | Computer token in hello | WebSocket upgrade (101), described below |

These are all registered routes (aiohttp also handles HEAD for GET routes).

### Calling a computer

```json
{"computer":3,"op":"exec","code":"return 6 * 7","reset":false,"timeout":30}
```

The body is an object: `op` names a bridge operation, and its arguments are **top-level fields**,
not an `args` object. `computer` selects the computer (`relay.resolve_target`):

| `computer` | Result |
|---|---|
| omitted or `null` | The only connected computer. None connected: `no_target` (409, `no computer is connected`); several: `no_target` (409), listing `#id (label)` pairs (`#3 (Workshop), #5 (no label)`) |
| a number | That id (an integral float such as `3.0` counts). Not connected: `not_connected` (404) |
| a non-empty string | The computer whose label equals it ignoring case (exact otherwise: no trimming or prefixes). Several such: `ambiguous` (409) with their ids. No label matches: a canonical id string (`"3"`, not `"03"`) is used as that id; anything else is `not_connected` (404) |
| anything else | `bad_request` (400): `""`, booleans, negative or fractional numbers, ids of 2^31 or more, arrays, objects |

Labels come from the hello and are updated whenever a `ping` through `/api/call` returns a different label; labels
quoted in error messages are JSON-escaped and cut to 32 characters. Labels only select a connection, they never
authenticate one. The MCP tools pass `computer` through unchanged; `ccctl --computer` sends a canonical id string as a
number (so `--computer 3` always means id 3) and any other text as a label.

The relay removes `computer`, reads `timeout` with `float()` (default 30 seconds), and forwards the
remaining body, `timeout` included, as the WebSocket request. Do not supply `type` or `req`: the current
encoder merges the body after those generated fields, so caller-supplied values would override them.
Unknown argument fields are not filtered. A missing or unknown `op` reaches the bridge, which answers
`bad_request`.

| HTTP status | When |
|---|---|
| 200 | Successful HTTP handling, including bridge failures and relay `too_large`, `relay_timeout`, `disconnected` |
| 400 | `/api/call` body cannot be parsed as JSON, or `computer` is neither an id nor a label (`bad_request`) |
| 401 | Missing or incorrect API bearer token on either API route (`unauthorized`) |
| 404 | No connected computer has that id or label (`not_connected`); unknown routes also get aiohttp's 404 |
| 409 | No `computer` given and zero or several computers connected (`no_target`), or several computers have that label (`ambiguous`) |
| 405 | Unsupported method (aiohttp routing response) |
| 413 | HTTP body exceeds aiohttp's default 1 MiB application body limit; not a protocol error object |
| 500 | Unhandled exceptions, as plain text: e.g. valid JSON that is not an object, or a `timeout` that is not a number (issue #5 is open) |

Protocol errors use `{"ok":false,"error":{"kind":"no_target","message":"..."}}`.
Framework errors need not be JSON. Inspect `ok` as well as HTTP status; a Lua failure normally has status 200.
The current Python client parses HTTP error bodies as JSON, so non-JSON framework failures raise locally (#5).

### Computer inventory

```json
{"computers":[{"id":3,"label":"Workshop","host":"ComputerCraft ...","connected_for_s":42,"remote":"127.0.0.1","auth":"own_token","replacements":0,"last_replaced_s_ago":null,"previous_remote":null}]}
```

`id` is numeric. `label` is the hello's label as last refreshed by a `ping` (see above) and `host` the hello's
`_HOST`; each is null when absent (a label that is not a string counts as absent).
`remote` is the relay's peer address (possibly null); `auth` is `own_token` or deprecated `shared_token`.
`connected_for_s` and non-null `last_replaced_s_ago` are rounded seconds. `replacements` counts successive
connections taking over this id from an open connection; it resets when there is no connection to replace.
`previous_remote` and `last_replaced_s_ago` are null until a replacement occurs. Tokens are never returned.

## WebSocket: bridge to relay

Connect to `/ws` using a text WebSocket. The first message must arrive within **10 seconds**:

```json
{"type":"hello","token":"<computer-token>","id":3,"label":"Workshop","host":"ComputerCraft ..."}
```

These are all the fields the bridge sends. `id` must be a JSON number representing a non-negative integer
below 2^31 (integral floats accepted, strings and booleans rejected). `token` is a nonempty matching string.
`label` is omitted when the computer has no label; `host` is `_HOST`. The relay does not require those two
metadata fields. The hello is serialised directly by CC, without the result `encoding` field.

The reply has only one field:

```json
{"type":"welcome"}
```

The bridge also waits 10 seconds for welcome; after timeout it reconnects. The relay configures aiohttp's
**30-second heartbeat** (WebSocket ping/pong frames, not JSON `ping` operations). The bridge reconnects 2 seconds
after the connection closes (including a 4401 close), 30 seconds after a first reply that is not `welcome`, and
with a delay doubling from 2 up to 30 seconds while connecting fails. After the handshake the relay only reads
text messages of type `result` and the bridge only messages of type `req`; anything else is ignored.

| Close code | Meaning |
|---|---|
| 4401, `unauthorized` | Missing/timed-out/malformed hello, invalid id or token; also an active computer whose token becomes invalid after reload |
| 4000, `replaced` | A newly authenticated connection claims the same id; the old connection is closed |

Per-computer tokens in `tokens.json`'s `computers` map are bound to canonical numeric id keys, e.g. `"3"`.
The deprecated shared `computer` token only works for ids without their own entry. The running relay checks
for file changes before hello authentication and API requests; invalid reloads keep the previous tokens.
Valid reloads recheck active connections and remove/close those no longer authorized. Public health/script
requests do not trigger reload. Environment token overrides still apply.

### Requests and results

```json
{"type":"req","req":1,"op":"read","path":"startup.lua","offset":0,"limit":100,"timeout":30}
```

`req` is a correlation number, normally a positive counter per connection. `op` is a string;
`timeout` is seconds for the bridge job timer (default 30 when absent). Arguments appear beside these fields.

```json
{"type":"result","req":1,"op":"read","ok":true,"data":"print(42)\n","size":10,"offset":0,"next_offset":10,"eof":true,"output":"","stderr":[],"duration":0.01,"encoding":"utf8"}
```

```json
{"type":"result","req":2,"op":"exec","ok":false,"error":{"kind":"syntax","message":"exec:1: unexpected symbol"},"output":"","stderr":[],"duration":0.01,"encoding":"utf8"}
```

| Result field | Meaning |
|---|---|
| `type`, `req`, `op` | `result`, the request correlation number, and the requested operation |
| `ok` | Boolean success; errors usually carry `error` |
| `error` | Object with `kind`, `message`, optional `traceback` (not guaranteed for every failure) |
| `encoding` | `utf8` or `bytes`; describes bridge serialisation, retained after relay normalisation |
| `output` | Captured terminal text for a job, including on failure |
| `stderr` | Array of captured `printError` strings for a job |
| `duration` | Job elapsed seconds measured with `os.clock()` |
| `truncated` | Present and true when capture or encoding shortened output/data |
| Operation-specific fields | Listed below; absent on most failures |

`ping`, `log`, `cancel` and immediate `busy` replies bypass jobs: they have no capture or duration fields.
An empty Lua array may be `{}` on the wire. The relay converts empty `stderr`, `returns`, `entries`,
`peripherals`, `lines`, `red`, `errors` and peripheral `types`/`methods` to `[]`.
Relay-generated errors have only `ok` and `error`, with no WebSocket envelope.

One ordinary request runs at a time per computer. The relay serialises them with a lock; `ping`, `log`
and `cancel` bypass it. The relay waits `timeout + 5` seconds **after** acquiring the lock. The Python HTTP
client waits `timeout + 15` seconds, including queue time. Long queues can therefore exhaust the client wait.
On a relay timeout for an ordinary op, the relay queues:

```json
{"type":"req","req":-1,"op":"cancel"}
```

The negative id is the negation of the timed-out request id. This is a separate request, with no timeout
field; its acknowledgement is logged, not delivered as the original result. No cancel is queued for
`ping`, `log` or `cancel` timeouts. Late original results are logged and not returned to the finished caller.
Cancellation abandons the job coroutine, not effects already performed. Timers require the game to tick.

### Bridge operations

Paths use CC's file system. Supply numbers for byte offsets/limits and booleans for flags; the bridge does
not comprehensively validate types or ranges. Defaults below apply to omitted fields.

| Op | Arguments | Successful fields beyond the envelope |
|---|---|---|
| `exec` | Required string `code`; `reset=false` | `returns`: array of Lua-serialised strings, not native JSON values. Tries an expression first, then statements; assignments persist in the session environment. `reset` replaces that environment before execution. |
| `shell` | Required string `command` | Job capture only; `shell.run` false becomes `exit`, using stderr or `command returned false`. |
| `read` | Required string `path`; `offset=0`, `limit=60000` (capped at 60000) | `data`, byte `size`, `offset`, `next_offset`, boolean `eof`. Binary-mode read; seek only when offset > 0. Page using `next_offset`, including after encoding truncation. |
| `write` | Required strings `path`, `data`; `append=false` | Total file `size` in bytes. Creates missing parent directories; overwrites unless append. This primitive does not stage a `.part` file. |
| `list` | `path=""` | `entries`: objects with `name`, `isDir`, `size` (0 for directories). |
| `peripherals` | `methods=false` | `peripherals`: objects with `name`, `types` array, optional `methods` array. |
| `log` | `lines=50` (maximum 300), `since_error=0`, `errors_only=false` | `lines`, `red` (1-based indices into lines), `errors` (`id`, `when`, `text`), `last_error_id`. Only errors with id > since_error; errors_only suppresses terminal lines. |
| `ping` | None | `id`, optional `label`, `clock` (os.clock), `day` (os.day), `host`, `free` (fs.getFreeSpace("/"), number or `"unlimited"`). A successful `ping` through `/api/call` also refreshes the relay's copy of the label. |
| `cancel` | None | `cancelled: true`, even if no job existed. An active job first receives its own `cancelled` failure. |

Log combines up to 600 scrolled-off lines with the current screen, omits blank lines and lines containing
`[bridge]`, and keeps the last requested lines. It retains 200 printError entries in memory; ids reset with
the bridge. `.bridge-errors.log` is also written, but `log` does not reread that file. Requests capture into a
100-column by 1000-row terminal; direct drawing to `term.native()` bypasses capture.

## Sizes and encodings

Limits are constants in `limits.py`, not environment settings:

| Limit | Value and behavior |
|---|---|
| CC default `http.websocket_message` | 131072 bytes in either direction |
| Relay encoded request | 100000 bytes; larger gives `too_large` (HTTP 200) before sending |
| Bridge encoded result | 120000 bytes; up to 10 encode attempts, halving `output` and `data` when longer than 1000 bytes |
| Relay incoming WebSocket message | 262144 bytes; aiohttp closes the connection on oversized input, not a structured `too_large` result |
| Capture / read before encoding | 60000 bytes each (Lua string lengths); capture keeps the tail, encoding shrink keeps a prefix |
| File-system snippet reply budget | 100000 bytes as encoded for the bridge; trims result lists and sets `cut` |

If a result still cannot fit (e.g. huge `returns`, listings or stderr), the bridge can send no result and the
relay times out. Failed serialisation is replaced with an `internal` result when that can be encoded.
Read shortening sets `eof=false` and updates `next_offset` using the shortened byte length.

Requests are JSON escaped to ASCII. The relay recursively replaces characters above U+FFFF in string
values with `?` before sending, because CC mis-decodes surrogate pairs; BMP text is sent as UTF-8 on CC.

For results, the bridge first tries `unicode_strings=true`, setting `encoding:"utf8"`. If any string has
invalid UTF-8 it falls back to `encoding:"bytes"`: JSON characters represent individual original bytes
(byte-wise escapes such as `\u00e9`), not base64. For example the UTF-8 bytes C3 A9 appear as `\u00c3\u00a9`.

Today the relay applies `fix_bytes` only to `output`, `data`, `stderr`/`returns`/`lines` elements,
`errors[].text`, and `error.message`/`error.traceback`. It tries Latin-1 encoding followed by UTF-8 decoding.
On failure it translates characters 0x80-0x9f to `-` (0x95 to `|`) and leaves other characters unchanged.
This includes **file data**, so a byte 0x95 can become `|`. Other strings (e.g. entry names) are not recursively
normalised. The `encoding` flag is retained: HTTP clients must not blindly decode these fields a second time.
**Issue #1 remains open:** this is not lossless binary transfer; its proposed byte-normalisation fix is not
implemented here. `pull_file` joins the returned strings and writes UTF-8, with no extra byte reconstruction.

## Operations composed by the Python client

These are not additional WebSocket op names. `ccctl --json` prints the client result, which may already be
normalised or assembled from several calls. Human-readable output uses `formatting.py`, wrapping computer
text in untrusted-data markers; those markers are not part of this wire protocol.

### Push and pull

`cc_push`, `ccctl push`, `ccctl write` and `ccctl update-bridge` use chunked text upload. `cc_write` instead
uses one `write` request. Push reads local UTF-8 text preserving newlines, checks free bytes/read-only/
directory conditions with an `exec` snippet, then sends `write` chunks. Each starts at 60000 characters and
halves until its JSON string fits 75000 bytes (or the chunk length reaches 500 or fewer). The request budget
reserves 20000 bytes for the envelope; long paths can still exceed the relay limit. Empty uploads still
send one chunk. Timeout is per request, not for the whole transfer.

Overwrites stage in `<remote>.part` (trailing slashes removed before adding `.part`), overwriting any stale
file there. The first write truncates, later writes append. One final `exec` deletes the old target and
moves the part into place. This is the client's atomic push strategy, but the delete/move is not a crash-safe
filesystem transaction: a failed move after deletion leaves the complete upload in the part file.
Append writes directly to the target and is not atomic. Cleanup after a failed upload is best effort.

Push results include `chars` (total Python characters), `sent_chars` (acknowledged), `chunks`, `part_left`
and total `duration`; success includes final byte `size`. Failure adds `remote_state`: `unchanged`,
`append` (possibly partial), `unknown` (swap not confirmed), or `part_kept` (old target deleted, upload kept).
`part_left` reports failed cleanup; use `remote_state` too, since `part_kept` can have `part_left:false`.

Pull repeats `read` using returned `next_offset` until `eof`, joins the data, writes a temporary file beside
the local destination, then calls `os.replace`. Success is `{"ok":true,"bytes":10,"path":"..."}`; `bytes`
is the final remote offset, not necessarily the UTF-8 size written locally. A failed pull preserves an
existing destination. MCP push/pull require `CC_MCP_FILES_ROOT`; ccctl uses paths as typed without that check.

### Snippets: inventory, peripheral calls and file-system tools

`cc_lua.py` builds code sent through `exec`; snippets return one JSON string inside the Lua-serialised
`returns[0]`. `parse_returned_json` decodes up to two JSON layers. Empty Lua lists may still be `{}` inside
this nested document. Network/call helpers retain the outer exec result; file-system helpers extract fields
into a new result with `ok` and optional `duration`, mapping snippet refusal reasons into error kinds.

| Helper | Inputs / nested result |
|---|---|
| `network` | `methods=false`; default timeout 60. `devices` (name/types, optional methods or methods_error), `modems` (side, optional local_name/wireless/remote_count), `local_names` (currently empty). |
| `run_calls` | List of `{peripheral, method, args}`; args optional, parallel default true. Packs p/m/args/n into the snippet. Result has `n`, `elapsed_ms`, `results`: each `{ok:true,values:[...]}` or `{ok:false,error:string}`. Individual failures do not make the outer exec fail. |
| `fs_find` | `pattern`, `max_results=200`; result `total`, `limit`, `entries` (path/isDir/optional size), optional `cut`. |
| `fs_grep` | `pattern`, `path=""`, `plain=true`, `max_matches=200`, `include_rom=false`; result `matches` (path/line/text), `files`, `binary`, `unreadable`, `limit`, optional `stopped` (`limit` or `time`), `rom_skipped`, `cut`. |
| `fs_stat` | `path`; result `exists`, and when available `isDir`, `isReadOnly`, `size` or directory `entries` count, `drive`, `capacity`, `free`, `created`, `modified` (epoch milliseconds). Missing path is successful `exists:false`. |
| `fs_delete` | `path`, `recursive=false`; result `path`, `isDir`, file `size` or directory `files`/`dirs`/`bytes` counts. Refuses root and nonempty directories without recursive. |
| `fs_move` / `fs_copy` | `src`, `dst` full destination path; result `from`, `to`, `isDir`, optional file `size`. Destination must not exist; missing parents are created. |
| `fs_mkdir` | `path`; result `path`, `existed`. Creates parents; an existing directory succeeds. |

Find/grep limits clamp to 1..1000 (default 200 on invalid conversion). Grep's time budget is
`max(0.25, timeout - min(5, timeout * 0.25))` seconds. Files with NUL in the first 1024 bytes are skipped;
rom is skipped unless requested or explicitly targeted. See [file-system usage](REFERENCE.md#file-system-tools).
File-system snippets independently use `bytewise:true` inside their nested JSON on invalid UTF-8; the client
recursively decodes that document as UTF-8 with replacement for invalid bytes. This is distinct from the
outer result's `encoding` flag and does not fix #1.

`CC_MCP_PROFILE` is a comma-separated, case-insensitive opt-in list, cached with other configuration.
Unknown profiles warn and are ignored. Currently `sable` adds optional `on_ship` and `ship_mass` to the network
document via protected sublevel API calls; missing APIs omit the fields. Profiles add no wire operations.
`CC_MCP_URL`, `CC_MCP_HOME`, `CC_MCP_API_TOKEN`, `CC_MCP_COMPUTER_TOKEN` are also cached configuration;
clients reread the token file on each call unless the API token override is set.

Relay INFO logs include request id, queue time, round-trip time, outcome and request/response byte counts.
DEBUG adds bounded code/command/path snippets; tokens are redacted. Logging outcomes such as `aborted` and
`unknown` are not error kinds promised to clients.

## Error kinds

`formatting.ERROR_KINDS` is the code registry; tests check this table against it. Origin identifies the
primary producer, not an authenticated provenance: a computer can claim any kind. Shared kinds note other
producers below. `200` means an error result carried by a successful `/api/call` response; `local` means a
client-generated result with no corresponding HTTP error status.

| Kind | Origin | HTTP status | Meaning |
|---|---|---|---|
| `syntax` | bridge | 200 | Exec code cannot compile. |
| `runtime` | bridge | 200; local mapping | Lua execution or primitive file failure; also client mapping of failed fs snippets. |
| `exit` | bridge | 200 | Shell command returned false; stderr supplies the message when present. |
| `timeout` | bridge | 200 | Bridge job timer expired and abandoned the job. |
| `terminated` | bridge | 200 | Execution raised an error ending in Terminated. |
| `cancelled` | bridge | 200 | Active job abandoned by a cancel request. |
| `internal` | bridge | 200; local | The request's coroutine crashed, the op returned no result, or the result could not be encoded; also the client when a snippet returns no or an unexpected answer. |
| `busy` | bridge | 200 | Another job is already active on the computer. |
| `disconnected` | relay | 200 | The computer's connection closed before it replied. The bridge also abandons a running job with this kind when its connection drops, but that result cannot be delivered. |
| `bad_request` | bridge | 200; 400; local | Missing op arguments or unknown op; the relay answers 400 for a body that is not JSON or an invalid `computer`; the client rejects malformed calls, unsafe fs paths or invalid patterns. |
| `relay_timeout` | relay | 200 | No reply within timeout plus relay margin; ordinary requests queue a cancel. |
| `too_large` | relay | 200 | Encoded outgoing request exceeds 100000 bytes. |
| `unauthorized` | relay | 401 | Incorrect/missing API token; WebSocket authorization instead closes with 4401. |
| `no_target` | relay | 409 | No `computer` given and not exactly one computer connected; the message lists `#id (label)` pairs. |
| `not_connected` | relay | 404 | No connected computer has the id or label given as `computer`. |
| `ambiguous` | relay | 409 | Several connected computers have the label given as `computer`; the message lists their ids. |
| `relay_unreachable` | client | local | HTTP connection/network error or client timeout. |
| `local_file` | client | local | Local file read/write/path permission failure, disabled MCP transfer, or non-UTF-8 upload/input file. |
| `no_space` | client | local | Push preflight reports too few free bytes. |
| `read_only` | client | local | Push or fs snippet refuses a read-only mount/path. |
| `is_directory` | client | local | Push target or temporary part is a directory. |
| `replace_failed` | client | local | Completed upload cannot replace target (part missing, delete failed or move failed). |
| `not_found` | client | local | Fs snippet reports a missing path. Primitive read/list use runtime instead. |
| `exists` | client | local | Move/copy destination exists, or mkdir target is a file. |
| `not_empty` | client | local | Delete requires recursive for a nonempty directory. |
