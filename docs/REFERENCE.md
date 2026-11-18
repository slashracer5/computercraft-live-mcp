# Technical reference

> **How to turn it on and connect: see [QUICKSTART.md](QUICKSTART.md).** This file is the technical reference.

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
| `src/computercraft_mcp/relay.py` | aiohttp relay: `/ws` (computer), `/api/call` (control), `/bridge.lua` (install), `/healthz` |
| `src/computercraft_mcp/bridge_client.py` | shared HTTP + file transfer and root checks |
| `src/computercraft_mcp/formatting.py` | per-operation formatters shared by MCP tools and ccctl; common header and computer-data emission helper |
| `src/computercraft_mcp/ccctl.py` | command-line client |
| `src/computercraft_mcp/mcp_server.py` | MCP tools: `cc_exec cc_shell cc_read cc_write cc_list cc_peripherals cc_log cc_cancel cc_ping cc_computers cc_network cc_call cc_batch cc_push cc_pull` |
| `src/computercraft_mcp/cc_lua.py` | Lua snippet builders and returned-JSON parser for `cc_network` / `cc_call` / `cc_batch` (run through `exec`) |
| `tests/test_*.py`, `tests/conftest.py` | pytest suite: helpers, the relay with a fake computer (in-process, free ports), file transfer; strict `xfail` tests document #1 and #5 |
| `tests/test_formatting.py` / `tests/test_formatting_clients.py` | formatter snapshots, error cases and MCP/CLI routing checks |
| `tests/selftest_cc.py` | end-to-end in headless CraftOS-PC incl. MCP client, reboot, relay restart; run by `tests/test_e2e_craftos.py` (marker `e2e`) or on its own; `CRAFTOS_PC` overrides the emulator path |
| `.github/workflows/ci.yml` | CI: ruff check / format check, pytest on Python 3.10–3.14 × Linux, Windows, macOS |

## Run

```
pip install git+https://github.com/slashracer5/computercraft-mcp
computercraft-mcp-relay --show-tokens          # tokens live in ~/.computercraft-mcp/tokens.json
computercraft-mcp-relay --host 127.0.0.1 --port 8765
```
In-game: `wget http://HOST:PORT/bridge.lua bridge.lua`, then `bridge ws://HOST:PORT/ws <computer-token>`.
For restarts put `shell.run("bridge")` in `startup.lua` (the url/token are saved in `.bridge.json`).
Update the bridge from the PC: `ccctl write bridge.lua --file <path to bridge.lua>`, then `ccctl exec "os.reboot()"`.

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
| `error.kind` | `syntax`, `runtime`, `exit`, `timeout`, `terminated`, `cancelled`, `busy`, `bad_request`, `internal` (relay adds `relay_timeout`, `disconnected`, `not_connected`, `too_large`) |
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
`peripherals`, `on_ship`, `computers`, `ping`, `call`, `call_error`, `result`).
Wrapped: file data, terminal output, return values, `stderr`, terminal and `printError` log lines, directory
listings, peripheral names/types/methods and modem info, computer labels and host strings (`ping`,
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
  Errors use the shared header and hints. Its CLI exit status remains 0, including on errors.
- `calls` (`cc_call` / `cc_batch`): elapsed time and each requested call's values or error.
  `network` and `calls` fall back to ordinary exec formatting when no matching JSON document is returned.
- `push`: uploaded chars, chunks and remote byte size. `pull`: saved bytes and local path.

Known formatters ignore unrelated fields; unknown operations use the header plus generic JSON.

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
