# Contributing

Keep each PR focused on one issue. For security problems, follow [SECURITY.md](SECURITY.md).

## Development setup

Use Python 3.10 or newer. From a checkout:

```sh
pip install -e ".[dev]"
```

The editable install uses your working copy, including `src/computercraft_live_mcp/bridge.lua`.
The relay reads that file fresh on every download request; no reinstall is needed after editing it.

## Tests and lint

```sh
pytest                       # all tests, including CraftOS-PC when found
pytest -m "not e2e"          # unit and relay tests only
python tests/selftest_cc.py  # CraftOS-PC end-to-end script on its own
ruff check .
ruff format --check .
```

`tests/test_e2e_craftos.py` runs the script with the `e2e` marker. Without an emulator,
the script exits 77 and pytest reports a skip with the reason.
It uses temporary folders and a free loopback port, never your own tokens or CraftOS-PC data.

New tests belong in `tests/test_<topic>.py`. Never bind fixed ports: use port 0 or aiohttp
test utilities. Mark async tests explicitly with `@pytest.mark.asyncio`.
Known bugs #1 (binary files through `cc_read` / `cc_pull`) and #5 (malformed or non-JSON API bodies and
responses) have strict `xfail` regression tests: drop the mark when fixing one.

Ruff configuration is in `pyproject.toml`: line length 120, target `py310`, rules E,F,W,I,B,UP.
Keep code compatible with Python 3.10, and avoid unrelated formatting changes.

`.github/workflows/ci.yml` runs `ruff check .` and `ruff format --check .` with ruff 0.17.5,
plus pytest on Python 3.10–3.14 across Linux, Windows and macOS.
CI does not install or run CraftOS-PC; its end-to-end test skips there.
Run it locally when changing `bridge.lua`, the relay, the client or tool output.

## CraftOS-PC setup

Install [CraftOS-PC](https://www.craftos-pc.cc). The test runs the console build headlessly.
It looks for the emulator at these defaults:

- Windows: `%LOCALAPPDATA%\CraftOS-PC\CraftOS-PC_console.exe`.
- Linux: `craftos` on PATH.
- macOS: `/Applications/CraftOS-PC.app/Contents/MacOS/craftos`.

Set `CRAFTOS_PC` to the executable path to override discovery (not the containing folder).
For example, in PowerShell:

```powershell
$env:CRAFTOS_PC = 'C:\path\to\CraftOS-PC_console.exe'
python tests/selftest_cc.py
```

The script exercises the real bridge, relay, MCP client and ccctl, including reconnects and reboot.
CraftOS-PC is a stand-in for CC:Tweaked; the in-game test remains the final word.

## Testing bridge changes in game

Run the relay from your editable install. Set up a test computer once (replace `ID` with the
number shown by `id` on that computer):

```sh
computercraft-live-mcp-relay --setup --computer ID
computercraft-live-mcp-relay
```

Use the printed commands to download and start the bridge with its per-computer token.
Apply the CC HTTP allow rule and autostart setup in [QUICKSTART.md](docs/QUICKSTART.md).
After each edit, while the old bridge is connected, run on the PC:

```sh
ccctl update-bridge
ccctl exec "os.reboot()"
```

Add `--computer ID_OR_LABEL` when several computers are connected. The reboot reports
`disconnected`, which is expected. With autostart installed, the new bridge reconnects.
Instead of rebooting, type `exit` then `bridge` on the computer to restart the bridge.

Alternatively, download the working copy directly on the in-game computer:

```text
rm bridge.lua
wget http://127.0.0.1:8765/bridge.lua bridge.lua
reboot
```

Use your relay's host/port if different. `wget` refuses to overwrite an existing file.
Create `.bridge-debug` on the computer to enable a connection trace in `.bridge-debug.log`.
Keep changes compatible with Lua 5.2 semantics and CC APIs. Keep mirrored constants in
`limits.py` and `bridge.lua` aligned; `tests/test_limits.py` checks them.

## Adding tools and errors

- A new MCP tool needs a row in the [README tool table](README.md#tools);
  `tests/test_docs.py` compares it with `list_tools()` in both directions.
- Add its ccctl subcommand, a row in [REFERENCE.md's ccctl table](docs/REFERENCE.md#command-line-ccctl),
  and an entry in `TOOL_TO_SUBCOMMAND` in `tests/test_ccctl.py`.
- A new error kind goes in `formatting.ERROR_KINDS`, the error-kind table in `docs/PROTOCOL.md` and the
  `error.kind` row in `docs/REFERENCE.md`; `tests/test_protocol_docs.py` checks all three. Usually add a hint
  (`formatting.HINTS`) too.
- Tool docstrings and `INSTRUCTIONS` are read by the model every session: keep them short and true.
  Send all computer data through the formatting helpers that wrap it in untrusted markers.

## Before opening a PR

- Keep one issue per PR and include `Closes #N` in the description.
- Add an entry under Unreleased in [CHANGELOG.md](CHANGELOG.md), ending with the issue number.
- Update docs with behavior changes; keep the README as the canonical tool inventory.
- Run ruff and pytest, and the local end-to-end test when relevant; report any skips.
- Never commit `tokens.json`, `.bridge.json` or logs.
