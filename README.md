# computercraft-mcp

Let an AI assistant (Claude Code, or any MCP client) work on a real **ComputerCraft: Tweaked** computer inside a running Minecraft world: run Lua, read and write files, call peripherals, and see every error, including from programs a player started by hand.

```
MCP client --stdio--> computercraft-mcp --HTTP + API token--> relay <--outbound websocket-- bridge.lua (in-game computer)
```

The in-game computer connects *out* to a small relay on your machine, so nothing needs to be exposed to the internet and the computer's own shell keeps working for players.

## Tools

| tool | what |
|---|---|
| `cc_exec` | run Lua (persistent session, returns values, terminal output and errors) |
| `cc_shell` | run a shell command / program |
| `cc_read` `cc_write` `cc_list` | files on the computer (paged reads, append) |
| `cc_find` `cc_grep` `cc_stat` | read-only file search: paths by glob, `path:line: text` matches, size/drive/free space |
| `cc_mkdir` `cc_copy` `cc_move` `cc_delete` | manage files without running code; never overwrite, and delete/move are marked destructive |
| `cc_push` `cc_pull` | upload / download files between your disk and the computer (opt-in, see below) |
| `cc_peripherals` `cc_network` | list peripherals (grouped by type, with methods, across wired modems) |
| `cc_call` `cc_batch` | call peripheral methods; a batch can run in parallel in one game tick |
| `cc_log` | recent terminal text plus every `printError`, with ids so you can poll only new errors |
| `cc_computers` `cc_ping` `cc_cancel` | which computers are connected, liveness, stop a stuck request |

Errors come back as data: an `ERROR` line, `error.kind`, message, traceback, hints for known causes and any `printError` output.

Computer data (file contents, output, log lines, labels, peripheral names, error messages) comes back inside `<untrusted source="..." id="...">` ... `</untrusted id="...">` blocks with a random id, so it cannot pose as the server's own lines. `ccctl` has a subcommand for every tool and prints the same text; add `--json` for the raw result.

## Quick start

```
pip install git+https://github.com/slashracer5/computercraft-mcp      # or: uv tool install git+https://...
computercraft-mcp-relay --setup --computer 3                       # starts nothing; gives computer #3 its own token, prints the in-game commands
computercraft-mcp-relay                                            # run the relay (leave it running)
```
(`id` on the in-game computer shows its id. Each computer gets its own token, which only works for that id.)

1. In CC:Tweaked's server config, allow the relay (rule goes **above** the default `$private` deny), then restart the world:
   ```toml
   [[http.rules]]
       host = "127.0.0.1"
       port = 8765
       action = "allow"
   ```
2. On the in-game computer, type the commands `--setup` printed (`wget .../bridge.lua bridge.lua`, then `bridge ws://127.0.0.1:8765/ws <token>`).
3. Register the MCP server with your client. For Claude Code, copy [`examples/mcp.json.example`](examples/mcp.json.example) to `.mcp.json` and set `CC_MCP_FILES_ROOT`.

Full walkthrough, troubleshooting and the protocol reference: [docs/QUICKSTART.md](docs/QUICKSTART.md), [docs/REFERENCE.md](docs/REFERENCE.md).

## Configuration

| variable | default | meaning |
|---|---|---|
| `CC_MCP_URL` | `http://127.0.0.1:8765` | relay address used by the MCP server and `ccctl` |
| `CC_MCP_HOME` | `~/.computercraft-mcp` | folder holding `tokens.json` (created on first run) |
| `CC_MCP_API_TOKEN` / `CC_MCP_COMPUTER_TOKEN` | from `tokens.json` | override the API token / the shared computer token (deprecated); never saved |
| `CC_MCP_FILES_ROOT` | *(unset: push/pull refuse)* | local folder(s) `cc_push` / `cc_pull` may touch, separated by `;` on Windows and `:` elsewhere (`ccctl` uses the paths you type) |

Message sizes and timeouts are defined in [src/computercraft_mcp/limits.py](src/computercraft_mcp/limits.py); see [Known limits](docs/REFERENCE.md#known-limits).

## Security

This is a remote shell for a game computer. Keep the relay on loopback or a private network, never share `tokens.json`, and read [SECURITY.md](SECURITY.md) before running it anywhere other than your own machine. Replace leaked tokens with `computercraft-mcp-relay --rotate api|computer|all`. Anything the computer sends back is untrusted data, and tool results mark it as such.

## Development

```
pip install -e ".[dev]"
ruff check . && ruff format --check .
pytest                        # everything, incl. the CraftOS-PC end-to-end test if the emulator is found
pytest -m "not e2e"           # quick run: unit and relay tests only
python tests/selftest_cc.py   # the end-to-end test on its own
```

The end-to-end test runs bridge.lua in headless CraftOS-PC (no Minecraft needed). It uses `CRAFTOS_PC` if set, otherwise `%LOCALAPPDATA%\CraftOS-PC\CraftOS-PC_console.exe` (Windows), `craftos` on PATH (Linux) or `/Applications/CraftOS-PC.app/Contents/MacOS/craftos` (macOS), and is skipped with the reason shown when it finds none. Tests use temporary folders and free loopback ports, never your own tokens or CraftOS-PC data.

CI runs ruff and pytest on Python 3.10–3.14 on Linux, Windows and macOS. Known bugs #1 and #5 have strict `xfail` regression tests: drop the mark when fixing them.

Tested with Python 3.14, CC:Tweaked on Minecraft 1.21.1 (NeoForge) and CraftOS-PC. MIT licensed.
