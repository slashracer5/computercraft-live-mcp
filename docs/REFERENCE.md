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
| `src/computercraft_mcp/bridge_client.py` | shared HTTP + result formatting + file-transfer root checks |
| `src/computercraft_mcp/ccctl.py` | command-line client |
| `src/computercraft_mcp/mcp_server.py` | MCP tools: `cc_exec cc_shell cc_read cc_write cc_list cc_peripherals cc_log cc_cancel cc_ping cc_computers cc_network cc_call cc_batch cc_push cc_pull` |
| `src/computercraft_mcp/cc_lua.py` | Lua snippet builders + formatters behind `cc_network` / `cc_call` / `cc_batch` (they run through `exec`, so `bridge.lua` is unchanged) |
| `tests/test_*.py`, `tests/conftest.py` | pytest suite: helpers, the relay with a fake computer (in-process, free ports), file transfer; strict `xfail` tests document #1 and #5 |
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
