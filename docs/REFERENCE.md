# Technical reference

> **How to turn it on and connect: see [QUICKSTART.md](QUICKSTART.md).** This file is the technical reference.

Lets an AI assistant run code and read files on an in-game ComputerCraft computer, and see every error,
including from programs a player started by hand.

```
MCP client --MCP (computercraft-live-mcp) / ccctl--> relay <--outbound websocket-- bridge.lua (in-game)
               HTTP + API token                 one port        computer token
```

## Files

| file | what |
|---|---|
| `src/computercraft_live_mcp/bridge.lua` | in-game client. Hosts the computer's shell, records the terminal, answers requests |
| `src/computercraft_live_mcp/relay.py` | aiohttp relay: `/ws` (computer), `/api/call` (control), `/bridge.lua` (install), `/healthz` |
| `src/computercraft_live_mcp/bridge_client.py` | shared by MCP tools and ccctl: HTTP calls, `network` / `run_calls` (with call validation), the `fs_*` file-system requests, chunked file transfer and root checks |
| `src/computercraft_live_mcp/formatting.py` | per-operation formatters shared by MCP tools and ccctl; common header and computer-data emission helper |
| `src/computercraft_live_mcp/ccctl.py` | command-line client: one subcommand per MCP tool, plus `update-bridge` |
| `src/computercraft_live_mcp/mcp_server.py` | MCP tools: `cc_exec cc_shell cc_read cc_write cc_list cc_peripherals cc_log cc_cancel cc_ping cc_computers cc_network cc_call cc_batch cc_push cc_pull cc_find cc_grep cc_stat cc_delete cc_move cc_copy cc_mkdir` |
| `src/computercraft_live_mcp/cc_lua.py` | Lua snippet builders and returned-JSON parser for `cc_network` / `cc_call` / `cc_batch`, the `cc_push` check/swap steps and the file-system tools (run through `exec`) |
| `tests/test_*.py`, `tests/conftest.py` | pytest suite: helpers, the relay with a fake computer (in-process, free ports), file transfer; strict `xfail` tests document #1 and #5 |
| `tests/test_formatting.py` / `tests/test_formatting_clients.py` | formatter snapshots, error cases and MCP/CLI routing checks |
| `tests/test_atomic_transfer.py` | `cc_push` / `cc_pull` atomicity against a fake computer (failed chunks, preflight refusals, local replace) |
| `tests/test_fs_tools.py` | file-system tools: snippet builders, refusals before sending, refusal-to-error mapping, formatters, MCP annotations, same text from ccctl |
| `tests/test_ccctl.py` | every MCP tool has a `ccctl` subcommand (and a row below), same request and text as the tool, exit status, `--json`, chunked `write`, `update-bridge` |
| `tests/selftest_cc.py` | end-to-end in headless CraftOS-PC incl. MCP client, reboot, relay restart; run by `tests/test_e2e_craftos.py` (marker `e2e`) or on its own; `CRAFTOS_PC` overrides the emulator path |
| `.github/workflows/ci.yml` | CI: ruff check / format check, pytest on Python 3.10–3.14 × Linux, Windows, macOS |

## Run

```
pip install git+https://github.com/slashracer5/computercraft-live-mcp
computercraft-live-mcp-relay --show-tokens          # tokens live in ~/.computercraft-live-mcp/tokens.json
computercraft-live-mcp-relay --host 127.0.0.1 --port 8765
```
In-game: `wget http://HOST:PORT/bridge.lua bridge.lua`, then `bridge ws://HOST:PORT/ws <computer-token>`.
For restarts put `shell.run("bridge")` in `startup.lua` (the url/token are saved in `.bridge.json`).
Update the bridge from the PC: `ccctl update-bridge` (uploads the `bridge.lua` shipped with the installed package to
`bridge.lua` on the computer, atomically), then `ccctl exec "os.reboot()"` (it reports `disconnected`: expected).

## Command line (`ccctl`)

`ccctl` sends the same requests as the MCP tools and prints the same text (markers included); `--json` prints the
raw result instead. Shared flags, before or after the subcommand: `--json`, `--computer ID`, `--timeout SECONDS`
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

## The hosted shell and the log tee

`bridge` runs a normal shell inside itself, so players keep using the computer. Everything drawn on the
terminal is mirrored into a shadow window; lines that scroll off are kept (600), and every `printError`
is recorded (200, also appended to `.bridge-errors.log` on the computer). `cc_log` / `ccctl log` returns
the recent terminal text (red lines marked `!`) and the `printError` entries with ids; pass
`since_error=<last_error_id>` to see only new ones. Type `exit` in the hosted shell to stop the bridge.
Only one bridge can run per computer (a second `bridge` refuses to start).
Create a file named `.bridge-debug` on the computer to get a connection trace in `.bridge-debug.log`.

## What comes back (every op)

| field | meaning |
|---|---|
| `ok` | false if the operation failed |
| `error.kind` | `syntax`, `runtime`, `exit`, `timeout`, `terminated`, `cancelled`, `busy`, `bad_request`, `internal` (relay adds `relay_timeout`, `disconnected`, `not_connected`, `too_large`; the client adds `relay_unreachable`, `local_file`, for `cc_push` `no_space`, `read_only`, `is_directory`, `replace_failed`, and for the file-system tools `not_found`, `exists`, `not_empty`, `read_only`) |
| `error.message` / `error.traceback` | Lua error text (`exec:3: boom`) and traceback |
| `stderr` | every `printError(...)` line from the request: how shell reports "No such program" and uncaught program errors |
| `output` | everything the code drew to the terminal (tail kept past 60 000 chars; `truncated` set) |
| `returns` | return values, Lua-serialised |
| `encoding` | `utf8`, or `bytes` when the data was not valid UTF-8 (binary files, CC border glyphs) |

Ops: `exec` (persistent session env, `reset` clears), `shell`, `read` (`offset`; `next_offset`, `eof`), `write`
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
`peripherals`, `on_ship`, `computers`, `ping`, `call`, `call_error`, `result`, `paths`, `matches`, `path`, `drive`).
Wrapped: file data, terminal output, return values, `stderr`, terminal and `printError` log lines, directory
listings, paths, matched lines and drive names from the file-system tools, peripheral names/types/methods and modem info, computer labels and host strings (`ping`,
`computers`), `cc_call`/`cc_batch` values and errors, the JSON of unknown operations, and every error
message and traceback. Error messages are wrapped even when the relay or the client wrote them: the
formatter cannot tell those apart from a computer reply that claims the same kind. The kind itself is a
server line (`  error: runtime`) only when it is in `formatting.KNOWN_KINDS`; any other kind goes inside
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
- Text is exact UTF-8 for characters up to U+FFFF; emoji become `?` going in (CC mis-decodes surrogate pairs).
- `read` returns at most ~60 KB per call, fewer for binary data (message cap 128 KiB): page with `next_offset`.
- Plain `ws://`, no TLS: keep the relay on the server VLAN / tailnet.
- The in-game CC config must allow the relay: an allow rule for host + port ABOVE `$private`.
- Anything coming back from the computer is untrusted data (players can write to signs, chat, files).
