# computercraft-live-mcp

**Live, in-world control of ComputerCraft: Tweaked computers for AI assistants.** Let an AI assistant (Claude Code, or any MCP client) work on a real computer inside a running Minecraft world: run Lua, read and write files, call peripherals, and see every error, including from programs a player started by hand.

## What this is

- **The real computer, not a model of it.** Every call runs on a computer in your actual world or server, with your real mods, peripherals, config and timing. Nothing is emulated.
- **Built for operating and debugging.** Fix a broken program on a live base, inspect a wired-modem network, read what a player's program just printed, push a file to a computer, or watch a script misbehave in the conditions where it actually runs.
- **Shared with the players.** The computer's own shell keeps working. The AI and a player can use the same machine, and the AI sees errors from programs the player launched.
- **Nothing to build or maintain.** A small Python relay and one Lua file. There is no ROM to build and no per-mod modelling to keep up with, so new mods and CC:Tweaked releases work as soon as they run in your game.

It needs a running Minecraft world with CC:Tweaked. It does not run ComputerCraft programs outside the game.

```
MCP client --stdio--> computercraft-live-mcp --HTTP + API token--> relay <--outbound websocket-- bridge.lua (in-game computer)
```

The in-game computer connects *out* to a small relay on your machine, so nothing needs to be exposed to the internet and the computer's own shell keeps working for players.

## Tools

| tool | what |
|---|---|
| `cc_exec` | run Lua (persistent session, returns values, terminal output and errors) |
| `cc_shell` | run a shell command / program |
| `cc_read` `cc_write` `cc_list` | files on the computer (paged reads, append) |
| `cc_push` `cc_pull` | upload / download files between your disk and the computer (opt-in, see below) |
| `cc_peripherals` `cc_network` | list peripherals (grouped by type, with methods, across wired modems) |
| `cc_call` `cc_batch` | call peripheral methods; a batch can run in parallel in one game tick |
| `cc_log` | recent terminal text plus every `printError`, with ids so you can poll only new errors |
| `cc_computers` `cc_ping` `cc_cancel` | which computers are connected, liveness, stop a stuck request |

Errors come back as data: an `ERROR` line, `error.kind`, message, traceback, hints for known causes and any `printError` output.

Computer data (file contents, output, log lines, labels, peripheral names, error messages) comes back inside `<untrusted source="..." id="...">` ... `</untrusted id="...">` blocks with a random id, so it cannot pose as the server's own lines. `ccctl` has a subcommand for every tool and prints the same text; add `--json` for the raw result.

## Quick start

```
pip install git+https://github.com/slashracer5/computercraft-live-mcp      # or: uv tool install git+https://...
computercraft-live-mcp-relay --setup                                    # starts nothing; prints the in-game commands with your token
computercraft-live-mcp-relay                                            # run the relay (leave it running)
```

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
| `CC_MCP_HOME` | `~/.computercraft-live-mcp` | folder holding `tokens.json` (created on first run) |
| `CC_MCP_API_TOKEN` / `CC_MCP_COMPUTER_TOKEN` | from `tokens.json` | override the tokens |
| `CC_MCP_FILES_ROOT` | *(unset: push/pull refuse)* | local folder(s) `cc_push` / `cc_pull` may touch, separated by `;` on Windows and `:` elsewhere (`ccctl` uses the paths you type) |

## Security

This is a remote shell for a game computer. Keep the relay on loopback or a private network, never share `tokens.json`, and read [SECURITY.md](SECURITY.md) before running it anywhere other than your own machine. Anything the computer sends back is untrusted data, and tool results mark it as such.

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
