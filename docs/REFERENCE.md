# Technical reference

> **How to turn it on and connect: see [QUICKSTART.md](QUICKSTART.md).** This file covers tool usage. Message schemas, HTTP statuses and the complete error catalogue are in [PROTOCOL.md](PROTOCOL.md).

Lets an AI assistant run code and read files on an in-game ComputerCraft computer, and see every error,
including from programs a player started by hand.

```
MCP client --MCP (computercraft-mcp) / ccctl--> relay <--outbound websocket-- bridge.lua (in-game)
               HTTP + API token                 one port        computer token
```

## Files

| file | what |
|---|---|
| `src/computercraft_mcp/bridge.lua` | in-game client. Hosts the computer's shell, records the terminal, answers requests |
| `src/computercraft_mcp/relay.py` | aiohttp relay: `/ws` (computer), `/api/call` (control), `/bridge.lua` (install), `/healthz`; `tokens.json` (`--setup`, `--rotate`) |
| `src/computercraft_mcp/bridge_client.py` | shared by MCP tools and ccctl: HTTP calls, `network` / `run_calls` (with call validation), the `fs_*` file-system requests, chunked file transfer and root checks |
| `src/computercraft_mcp/formatting.py` | per-operation formatters shared by MCP tools and ccctl; common header and computer-data emission helper |
| `src/computercraft_mcp/config.py` | frozen environment configuration, cached on first use; `reset_config()` clears it |
| `src/computercraft_mcp/limits.py` | message sizes, push chunk budgets, the file-system tools' reply limit and timeout defaults; tests check Lua copies |
| `src/computercraft_mcp/ccctl.py` | command-line client: one subcommand per MCP tool, plus `update-bridge` |
| `src/computercraft_mcp/mcp_server.py` | MCP tools: `cc_exec cc_shell cc_read cc_write cc_list cc_peripherals cc_log cc_cancel cc_ping cc_computers cc_network cc_call cc_batch cc_push cc_pull cc_find cc_grep cc_stat cc_delete cc_move cc_copy cc_mkdir` |
| `src/computercraft_mcp/cc_lua.py` | Lua snippet builders and returned-JSON parser for `cc_network` / `cc_call` / `cc_batch`, the `cc_push` check/swap steps and the file-system tools (run through `exec`); opt-in mod profiles |
| `tests/test_*.py`, `tests/conftest.py` | pytest suite: helpers, the relay with a fake computer (in-process, free ports), file transfer; strict `xfail` tests document #1 and #5 |
| `tests/test_formatting.py` / `tests/test_formatting_clients.py` | formatter snapshots, error cases and MCP/CLI routing checks |
| `tests/test_atomic_transfer.py` | `cc_push` / `cc_pull` atomicity against a fake computer (failed chunks, preflight refusals, local replace) |
| `tests/test_fs_tools.py` | file-system tools: snippet builders, refusals before sending, refusal-to-error mapping, formatters, MCP annotations, same text from ccctl |
| `tests/test_ccctl.py` | every MCP tool has a `ccctl` subcommand (and a row below), same request and text as the tool, exit status, `--json`, chunked `write`, `update-bridge` |
| `tests/test_labels.py` | computer labels: relay target resolution, `ambiguous` / `no_target` messages, ping label refresh, every tool's `computer` accepts a label, ccctl `--computer` |
| `tests/test_protocol_docs.py` | the error-kind table in [PROTOCOL.md](PROTOCOL.md) matches `formatting.ERROR_KINDS`, and every kind the code returns is in it |
| `tests/selftest_cc.py` | end-to-end in headless CraftOS-PC incl. MCP client, reboot, relay restart; run by `tests/test_e2e_craftos.py` (marker `e2e`) or on its own; `CRAFTOS_PC` overrides the emulator path |
| `.github/workflows/ci.yml` | CI: ruff check / format check, pytest on Python 3.10–3.14 × Linux, Windows, macOS |

## Run

```
pip install git+https://github.com/slashracer5/computercraft-mcp
computercraft-mcp-relay --setup --computer 3   # token for computer #3 + the in-game commands
computercraft-mcp-relay --show-tokens          # tokens live in ~/.computercraft-mcp/tokens.json
computercraft-mcp-relay --rotate computer --computer 3   # or: --rotate api|computer|all
computercraft-mcp-relay --host 127.0.0.1 --port 8765
```
Relay logging flags: `--log-level DEBUG|INFO|WARNING|ERROR` (case-insensitive, default `INFO`)
and `--log-file PATH` (append UTF-8 logs to a file as well as stderr). `--setup`, `--rotate` and
`--show-tokens` print their output to stdout and exit; `--host`/`--port` set the address they print.

In-game: `wget http://HOST:PORT/bridge.lua bridge.lua`, then `bridge ws://HOST:PORT/ws <computer-token>`.
For restarts put `shell.run("bridge")` in `startup.lua` (the url/token are saved in `.bridge.json`).
Update the bridge from the PC: `ccctl update-bridge` (uploads the `bridge.lua` shipped with the installed package to
`bridge.lua` on the computer, atomically), then `ccctl exec "os.reboot()"` (it reports `disconnected`: expected).

## Choosing a computer

Tools accept `computer=<id or label>`; labels match exactly ignoring case (no trimming or prefixes).
A digit string like `"3"` falls back to a valid id only if no label matches (label wins; `"03"` stays a label).
Duplicates give `ambiguous` (409); missing ids or labels give `not_connected` (404). Use `cc_computers` to list both.
Omit `computer` with one connected; otherwise `no_target` (409) reports none or lists `#id (label)` pairs.
Labels refresh on connect and by `cc_ping`; after changing one, ping by id first.
ccctl `--computer 3` means id 3; relay `--setup` / `--rotate` still require numeric ids.

## Command line (`ccctl`)

`ccctl` sends the same requests as the MCP tools and prints the same text (markers included); `--json` prints the
raw result instead. Shared flags, before or after the subcommand: `--json`, `--computer ID_OR_LABEL`, `--timeout SECONDS`
(per request; default 30, `network` 60). Exit status is 1 when the relay or the operation fails, including
`computers` (relay down, wrong token) and `call` / `batch` when any call fails; otherwise 0.

| MCP tool | ccctl |
|---|---|
| `cc_computers` | `ccctl computers` |
| `cc_ping` | `ccctl ping` |
| `cc_exec` | `ccctl exec CODE [--reset]` |
| `cc_shell` | `ccctl shell COMMAND` |
| `cc_read` | `ccctl read PATH [--offset N]` |
| `cc_write` | `ccctl write PATH [--file LOCAL] [--append]` (text from stdin without `--file`) |
| `cc_list` | `ccctl ls [PATH]` |
| `cc_peripherals` | `ccctl periph [--methods]` |
| `cc_log` | `ccctl log [--lines N] [--errors-only] [--since-error ID]` |
| `cc_cancel` | `ccctl cancel` |
| `cc_network` | `ccctl network [--methods]` |
| `cc_call` | `ccctl call PERIPHERAL METHOD [ARG ...]`: each ARG is parsed as JSON (`5`, `true`, `'"5"'`, `'[1,2]'`), anything else is a string |
| `cc_batch` | `ccctl batch [CALLS \| --file CALLS.json] [--sequential]`: a JSON list as for `cc_batch` (stdin when neither is given); parallel unless `--sequential` |
| `cc_push` | `ccctl push LOCAL REMOTE [--append]` |
| `cc_pull` | `ccctl pull REMOTE LOCAL` |
| `cc_find` | `ccctl find PATTERN [--max N]` |
| `cc_grep` | `ccctl grep PATTERN [PATH] [--lua] [--max N] [--include-rom]`: plain text unless `--lua` |
| `cc_stat` | `ccctl stat PATH` |
| `cc_delete` | `ccctl rm PATH [--recursive]` |
| `cc_move` | `ccctl mv SRC DST` |
| `cc_copy` | `ccctl cp SRC DST` |
| `cc_mkdir` | `ccctl mkdir PATH` |
| *(no MCP tool)* | `ccctl update-bridge`: upload the packaged `bridge.lua`, then print how to restart the bridge |

- `write` uploads like `cc_push` (`bridge_client.push_text`): chunked, so any size works, and an overwrite is atomic
  through `<path>.part`. It prints the `push` text (`uploaded N chars in K chunk(s); remote size N bytes`).
- Local paths given to `write --file`, `push` and `pull` are used as typed (relative to the current folder):
  `CC_MCP_FILES_ROOT` only limits the MCP tools, which act for the model. On the command line you chose the file.
- `computers`, `call`, `batch`, `network` and the push/pull text come from the same `bridge_client` /
  `formatting` functions the MCP tools use, so a problem seen through MCP can be reproduced from a shell.

## Tokens

`tokens.json` (in `CC_MCP_HOME`):

```
{"api": "<API token>", "computer": "<shared computer token, deprecated>", "computers": {"3": "<token for #3>"}}
```

- The bridge's hello carries its token and `os.getComputerID()`. The relay accepts it only if the token is
  that id's own token. The shared `computer` token is still accepted, with a deprecation warning, for ids
  that have no entry in `computers`; once an id has its own token, the shared token is refused for it.
  Ids must be integers from 0 to 2^31-1; anything else is refused.
- `--setup --computer ID` issues (or reuses) the token for that id. `--rotate api` replaces the API token,
  `--rotate computer` the shared token and every per-computer token (`--computer ID`: only that one),
  `--rotate all` everything. Both print what to update (the in-game `bridge` line, `CC_MCP_API_TOKEN`).
- A running relay checks whether `tokens.json` changed before each hello and API request. It then reloads
  it and disconnects computers whose token is no longer valid; a file it cannot use is ignored with a
  warning and the previous tokens stay in force.
- `CC_MCP_API_TOKEN` and `CC_MCP_COMPUTER_TOKEN` override `api` and the shared `computer` token; overrides
  are never written to the file. Per-computer tokens have no override.
- The file is created with `O_CREAT | O_EXCL` and mode `0600` (its folder `0700`); updates go to a `0600`
  temp file in the same folder and replace the file atomically. On Windows it inherits the folder's
  permissions: keep `CC_MCP_HOME` in your user profile. A malformed file stops the relay with a message.
- `ccctl` and the MCP server read the `api` token from the file on every call.

## Relay log

At INFO, each API call has one summary with computer, operation, request id, time queued for
the computer's lock, round-trip time, outcome, and encoded request/received websocket response sizes:

```
#3 exec req=12 queued=0.00s rtt=0.31s outcome=runtime req_bytes=118 resp_bytes=402
```

Sizes are bytes; `resp_bytes=-` means no reply. Calls rejected before selecting a computer also
get a summary, with `-` for unavailable fields. Queue time is zero for `ping`, `cancel` and `log`.
The relay's timeout starts after acquiring the lock; queue time is unbounded and can outlast
the HTTP caller's timeout. INFO also records connections (id, label, peer address), disconnects
(duration, close code) and acknowledgements of queued cancels.

WARNING records `relay_timeout` with queue time, requested timeout, relay wait and whether a cancel
was queued; queued cancels, replaced connections (old and new peer address, replacement count), oversized
requests, rejected hellos (peer address, claimed id and reason: `not JSON`, `not a hello`, `bad id`,
`wrong token`, `token not valid for this id (it is computer #N's token)`, `shared token not valid for
this id (it has its own token)`, `no hello within 10 s`), connections that use the deprecated shared
token, computers disconnected because `tokens.json` changed, unusable `tokens.json` edits, unauthorized
API requests, replies that arrive after the relay gave up (`answered late`, with how late) and
unexpected replies.

`/api/computers` (`cc_computers`, `ccctl computers`) lists, per computer: `id`, `label`, `host`,
`connected_for_s`, `remote` (peer address), `auth` (`own_token`, or `shared_token` while the
deprecated token is in use), `replacements` (how many times a new connection took over this id from an
open one; a reboot can cause one), `last_replaced_s_ago` and `previous_remote` (the address of the
connection it replaced; `null` when there was none).

Tokens are never logged. INFO and above omit code, commands, file contents and returned values.
DEBUG adds paths and code/command snippets (up to 200 characters, ASCII-escaped, with the full
length), and only the length of write data. Treat DEBUG log files like the code itself.
Third-party warnings and errors share the log; routine aiohttp access logs are suppressed.

## Mod profiles

`CC_MCP_PROFILE` (comma-separated, case-insensitive, read once when the MCP server starts) enables entries of
`cc_lua.PROFILES`; unknown names are skipped with a warning on stderr. Without a profile no tool description,
output or instruction names a mod. A `Profile` can add:

- `network_probe`: Lua appended to `cc_network`'s snippet in its own `do ... end` block, after the inventory;
  it may set fields on `out` (wrap mod API calls in `pcall`, the API may be missing);
- `network_fields`: those fields, shown on `cc_network`'s peripheral count line when present (booleans and
  numbers as they are, any other value in an `<untrusted>` block named after the field);
- `network_notes`: lines added to the end of `cc_network`'s output (not `cc_peripherals`');
- `instructions`: text appended to the MCP server instructions.

`sable` probes `sublevel.isInPlotGrid()` (`on_ship`) and `sublevel.getMass()` (`ship_mass`); see the README.

## The hosted shell and the log tee

`bridge` runs a normal shell inside itself, so players keep using the computer. Everything drawn on the
terminal is mirrored into a shadow window; lines that scroll off are kept (600), and every `printError`
is recorded (200, also appended to `.bridge-errors.log` on the computer). `cc_log` / `ccctl log` returns
the recent terminal text (red lines marked `!`) and the `printError` entries with ids; pass
`since_error=<last_error_id>` to see only new ones. Type `exit` in the hosted shell to stop the bridge.
Only one bridge can run per computer (a second `bridge` refuses to start).
Create a file named `.bridge-debug` on the computer to get a connection trace in `.bridge-debug.log`.

## Result fields (when applicable)

| field | meaning |
|---|---|
| `ok` | false if the operation failed |
| `error.kind` | `syntax`, `runtime`, `exit`, `timeout`, `terminated`, `cancelled`, `busy`, `bad_request`, `internal` (relay adds `relay_timeout`, `disconnected`, `too_large`, `unauthorized`, `no_target`, `not_connected`, `ambiguous`; the client adds `relay_unreachable`, `local_file`, for `cc_push` `no_space`, `read_only`, `is_directory`, `replace_failed`, and for the file-system tools `not_found`, `exists`, `not_empty`, `read_only`). Origins, HTTP statuses and meanings: [error kinds](PROTOCOL.md#error-kinds) |
| `error.message` / `error.traceback` | Lua error text (`exec:3: boom`) and traceback |
| `stderr` | every `printError(...)` line from the request: how shell reports "No such program" and uncaught program errors |
| `output` | everything the code drew to the terminal (tail kept past 60 000 bytes before encoding; further size reduction may keep a prefix; `truncated` set) |
| `returns` | return values, Lua-serialised |
| `encoding` | `utf8`, or `bytes` when the data was not valid UTF-8 (binary files, CC border glyphs) |

Ops: `exec` (persistent session env, `reset` clears), `shell`, `read` (`offset`, `limit`; `next_offset`, `eof`), `write`
(`append`), `list`, `peripherals`, `log`, `ping`, `cancel`.

## Text output

`formatting.format_result(op, res, calls=...)` selects an operation from `FORMATTERS`.
MCP tools and `ccctl` use it for all human-readable results; `ccctl --json` prints the raw result.
The first line is `OK` or `ERROR`, with duration appended when present (`OK [0.12s]` / `ERROR [1.25s]`).
Next come any hint, error kind, error message/traceback and `stderr`, in that order.
Job results retain terminal `output:` even on failure, including the `(truncated, tail kept)` note.
Durations and read paging instructions precede the computer data they describe.

Computer data is wrapped in markers so that it cannot pass itself off as the server's own lines:

```
OK [0.05s]
[more data: call again with offset=60000]
<untrusted source="file" id="3f9a0c1b">
...file contents, verbatim...
</untrusted id="3f9a0c1b">
```

Every block gets a fresh random 8-hex-digit id that does not occur in its content, so the content cannot
close the block early. A one-line value inside a server line uses the same markers on that line
(`returns: <untrusted source="returns" id="…">42</untrusted id="…">`). `source` is a fixed word chosen by
the formatter (`file`, `output`, `returns`, `stderr`, `error`, `listing`, `terminal`, `printError`,
`peripherals`, a profile field's name such as `on_ship`, `computers`, `ping`, `call`, `call_error`, `result`, `paths`, `matches`, `path`, `drive`).
Wrapped: file data, terminal output, return values, `stderr`, terminal and `printError` log lines, directory
listings, paths, matched lines and drive names from the file-system tools, peripheral names/types/methods and modem info, computer labels and host strings (`ping`,
`computers`), `cc_call`/`cc_batch` values and errors, the JSON of unknown operations, and every error
message and traceback. Error messages are wrapped even when the relay or the client wrote them: the
formatter cannot tell those apart from a computer reply that claims the same kind. The kind itself is a
server line (`  error: runtime`) only when it is in `formatting.ERROR_KINDS`; any other kind goes inside
the error block. Status, hints, paging instructions, counts, sizes and durations stay outside the blocks.
`ccctl` prints the same text, markers included; `ccctl --json` prints the raw result without markers.

- `exec` / `shell`: return values (`returns: 42`) and captured output.
- `read`: file data, preceded by `[more data: call again with offset=N]` whenever `eof` is false.
- `write`: remote size in bytes. `list`: `d name/` for directories, right-aligned byte size then name for files.
- `peripherals` / `network`: names grouped by type and methods, with methods listed once per group.
  `network` also includes modem and ship information. Names and methods are sorted within groups.
  Types retain the computer's reported order, with the primary type first.
- `log`: `last_error_id`, terminal lines (`!` marks red text), and `printError` entries with id and time.
- `ping`: id, label, clock, day, host and free space. `cancel`: the bridge's reported `cancelled` flag.
- `computers`: one line per computer with id, label, host and `connected_for_s`, or `No computers connected.`
  Errors use the shared header and hints; `ccctl computers` then exits 1.
- `calls` (`cc_call` / `cc_batch`): elapsed time and each requested call's values or error.
  `network` and `calls` fall back to ordinary exec formatting when no matching JSON document is returned.
- `push`: uploaded chars, chunks and remote byte size. A failed push says how far it got and what is on the
  computer now (`sent 60000 of 150000 chars in 1 chunk(s); the remote file is unchanged`).
  `pull`: saved bytes and local path.
- `find`: the number of matching paths (`200 of 1534 matching paths shown (max_results=200)` when capped), then the
  paths in a `paths` block, formatted like `list`.
- `grep`: `N match(es) in M file(s)` plus notes (rom/ skipped, binary files skipped, stopped at `max_matches` or
  early for the timeout), then `path:line: text` lines in a `matches` block.
- `stat`: `exists`, `type` (file and size, or directory and entry count), `read-only`, `drive` (in a block),
  `capacity`, `free`, `created` / `modified` (UTC), each on its own line when the computer reported it.
- `delete` / `move` / `copy` / `mkdir`: one line saying what was done, with the paths in an inline block:
  `deleted directory (3 file(s), 1 subdirectory(s), 120 bytes): ...`, `moved file (42 bytes): a.lua -> b.lua`,
  `created directory: ...` or `directory already exists: ...`.

Known formatters ignore unrelated fields; unknown operations use the header plus generic JSON.

## File transfer (`cc_push` / `cc_pull`)

Both only touch local folders listed in `CC_MCP_FILES_ROOT` (`ccctl push` / `pull` / `write --file` use the path
you type; see above).

- `cc_push` first runs one small `exec` check: it refuses a read-only target, a target that is a directory,
  and an upload whose UTF-8 size exceeds the free space on the target's drive (`fs.getFreeSpace`; an overwrite
  needs that room because the new file and the old one coexist until the swap). Nothing is written when the
  check fails.
- An overwrite then writes the chunks to `<remote>.part` (a stale one is overwritten) and finishes with one
  `exec` that deletes the old file and moves the part into place. If a chunk fails, the part is deleted
  (best effort) and the old file is untouched; if the connection is gone, the part stays behind until the next
  push to that path overwrites it. A file of your own named `<remote>.part` is overwritten too.
- `append=True` runs the same check, then appends straight to the file: not atomic.
- `cc_pull` downloads everything first, writes a temp file in the destination folder, then `os.replace`s it
  over the destination, so a failed pull leaves an existing local file as it was.

## File-system tools

`cc_find`, `cc_grep` and `cc_stat` are read-only (`readOnlyHint`), so a client can auto-approve them for exploring
the computer. `cc_delete`, `cc_move`, `cc_copy` and `cc_mkdir` change files without running arbitrary code and are
annotated so an approval prompt can tell them apart: `cc_delete` and `cc_move` have `destructiveHint=true` (a move
removes the old path); `cc_copy` and `cc_mkdir` only add (`destructiveHint=false`; `cc_mkdir` is also idempotent).
All seven are `cc_lua` snippets run through `exec`, so `bridge.lua` is unchanged and they wait behind a running
request like everything else.

- `cc_find(pattern, max_results=200)`: CC's `fs.find` glob. `*` matches within one path segment (`*/*.lua`); there
  is no recursive `**`. Sorted; at most `max_results` (1000 max) paths with their size, plus the total count.
- `cc_grep(pattern, path="", plain=True, max_matches=200, include_rom=False, timeout=30)`: searches one file, or
  every file under a directory, line by line. `plain=False` makes `pattern` a Lua pattern (an invalid one is a
  `bad_request`). `rom/` is skipped unless `include_rom=True` or `path` is inside it. Files with a NUL byte in their
  first 1 KiB count as binary and are skipped. Lines longer than 200 bytes are cut to 200 bytes around the match
  (`...` marks the cuts). The search yields every 200 ms (so CC's "too long without yielding" abort cannot hit and
  `cc_cancel` and the timeout still work) and stops by itself at `max_matches` or shortly before `timeout`,
  returning what it found and saying why it stopped.
- `cc_stat(path)`: `fs.exists`, `fs.isDir`, `fs.getSize` (or a directory's entry count), read-only (see below),
  `fs.getDrive`, `fs.getCapacity`, `fs.getFreeSpace` and `fs.attributes` (created/modified), each in a `pcall` so a
  CC version without one of them still answers. A missing path is not an error: `exists: false`.
- `cc_delete(path, recursive=False)`: refuses the root directory (`""`, `/`, `a/..`, or anything above the root)
  before sending and again on the computer, a missing path (`not_found`), a read-only one (`read_only`) and a
  non-empty directory unless `recursive=True` (`not_empty`). A recursive delete reports how many files,
  subdirectories and bytes it removed.
- `cc_move(src, dst)` / `cc_copy(src, dst)`: `dst` is the new full path. It must not exist (`exists`: CC's `fs.move`
  and `fs.copy` never overwrite); its missing parent folders are created; a read-only destination (or a read-only
  source of a move) is refused. Copying out of `rom/` works.
- `cc_mkdir(path)`: `fs.makeDir`, parents included. An existing directory is success (`directory already exists`);
  an existing file is `exists`.

Read-only means "on a read-only mount" (`rom/`, a treasure disk): the tools ask `fs.isReadOnly` about the root folder
of the mount that holds the path (or, for a path that does not exist yet, its nearest existing parent). CC's
read-only paths are whole mounts, and CraftOS-PC's `fs.isReadOnly` wrongly answers true for writable folders created
by `fs.makeDir("a/b")` and for new paths beside them.

Text that is not valid UTF-8 (a Latin-1 file matched by `cc_grep`, odd file names) cannot go through the Unicode
mode of `textutils.serialiseJSON`: the snippet then sends its answer byte by byte and the client decodes it back,
so valid UTF-8 survives and only the invalid bytes become U+FFFD. Every answer is trimmed to fit one bridge message,
and the result says when paths or matches were left out.

## Known limits

- One request at a time; while it runs, the player's keystrokes are held and replayed afterwards.
  `ping`, `log` and `cancel` always work. A request that blocks (`sleep(30)`) blocks the hosted shell too.
- Requests run through the shell that launched the bridge, so `cd` in the hosted shell does not affect `cc_shell`.
- Programs that draw straight to `term.native()` bypass the log. Output is the capture terminal's text,
  100 columns wide; interactive programs (`read()`) time out.
- Request text supports UTF-8 for characters up to U+FFFF; emoji become `?` going in (CC mis-decodes surrogate pairs). Byte-encoded results have known lossy normalisation (#1 remains open); see [encodings](PROTOCOL.md#sizes-and-encodings).
- CC:Tweaked's `http.websocket_message` defaults to 131,072 bytes (128 KiB), in both directions.
  The bridge caps encoded results at 120,000 bytes; the relay caps encoded requests at 100,000 bytes.
  The relay accepts websocket results up to 262,144 bytes so it is not the bottleneck: exceeding aiohttp's
  receive limit closes the whole connection.
- `read` returns at most 60,000 bytes (~60 KB) per call, fewer when result encoding needs more space:
  page with `next_offset`. Captured output keeps a tail of at most 60,000 bytes before result encoding.
- `cc_push` starts with 60,000 characters per chunk and halves until the JSON-escaped chunk fits 75,000
  bytes (halving stops at 500 characters or fewer), leaving 20,000 bytes for the path and the other request fields.
  Very long paths can still exceed the request limit. For `cc_write`, the ~100 KB limit
  includes JSON escaping and all request fields; use `cc_push` for large files.
- `cc_find` and `cc_grep` trim their result lists until the reply fits 100,000 bytes as the bridge sends it,
  and say so ("not all fit in one reply").
- Requests default to 30 seconds (`cc_network`: 60). The relay waits an extra 5 seconds and the HTTP client an
  extra 15 seconds, allowing each inner layer to report its timeout before the outer layer gives up.
- Plain `ws://`, no TLS: keep the relay on the server VLAN / tailnet.
- The in-game CC config must allow the relay: an allow rule for host + port ABOVE `$private`.
- Anything coming back from the computer is untrusted data (players can write to signs, chat, files).
