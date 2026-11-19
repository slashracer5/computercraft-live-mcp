# Changelog

All notable changes are documented here, following [Keep a Changelog 1.1.0](https://keepachangelog.com/en/1.1.0/).
This project follows [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added

- GitHub Actions CI: ruff check/format and pytest on Python 3.10–3.14 across Linux, Windows and macOS; pytest replaces the relay selftest, with a `dev` extra and ruff configuration (120 columns; E,F,W,I,B,UP). CraftOS-PC tests use `CRAFTOS_PC` or OS defaults, a free port, and exit 77 when unavailable. (#20)
- File-system tools `cc_find`, `cc_grep`, `cc_stat`, `cc_delete`, `cc_move`, `cc_copy`, `cc_mkdir`, with MCP annotations, ccctl equivalents and `not_found`, `exists`, `not_empty` errors. (#14)
- ccctl subcommands for every MCP tool, including network, call, batch, push and pull, plus `update-bridge`. (#16)
- Shared cached environment configuration in `config.py` and limits in `limits.py`, with tests checking the mirrored Lua limits. (#22)
- Relay `--log-level` and `--log-file`, per-call INFO timing/outcome/size logs, and warnings for timeouts, late replies, replaced connections and rejected hellos; tokens are never logged. (#19)
- Wire protocol reference in `docs/PROTOCOL.md` covering websocket/HTTP schemas, operation arguments/results and errors; `formatting.ERROR_KINDS` and tests keep the error inventory aligned. (#24)
- `computercraft-live-mcp-relay --rotate api|computer|all` (`--computer ID` for one computer) replaces tokens and prints what to update. (#17)
- `CONTRIBUTING.md` and this changelog. README's tool table is the only tool list; a test, also used by the CraftOS-PC selftest, checks it against the server's tools. (#25)

### Changed

- Per-operation result formatters shared by MCP and ccctl, compact directory/peripheral listings, and duration in the status line (`OK [0.12s]`). (#21)
- `cc_push` overwrites via `<remote>.part` and a single swap after space/read-only/directory preflight; failures use `no_space`, `read_only`, `is_directory` or `replace_failed`. `cc_pull` uses a temporary file and `os.replace()`. (#13)
- `ccctl write` uploads in chunks without a single-request size cap and overwrites atomically; failures exit 1, including unavailable/unauthorized computer listings and any failed call in call/batch. (#16)
- Every tool's `computer` and `ccctl --computer` accept an id or case-insensitive exact label; duplicate labels return `ambiguous`, and `no_target` lists `#id (label)` pairs. (#15)
- The server carries no mod-specific behaviour: Sable's `on_ship` / `ship_mass` and the Create Avionics notes were removed from `cc_network` and the instructions. (#23)
- **Breaking:** `computercraft-live-mcp-relay --setup` requires `--computer ID` and issues that computer its own token. `/api/computers` (`cc_computers`) adds `remote`, `auth`, `replacements`, `last_replaced_s_ago` and `previous_remote`. (#17)

### Deprecated

- Shared computer tokens: still accepted with a warning for ids without their own token. (#17)

### Fixed

- Tool descriptions and server instructions: how `cc_exec` and `cc_call` values are serialised, which programs `cc_shell` cannot run, what counts towards `cc_write`'s size limit, and `cc_log`'s 300-line cap. The in-game bridge update says to `rm bridge.lua` first, since `wget` never overwrites. (#25)

### Security

- Computer data and every error message are wrapped in `<untrusted source="..." id="...">` … `</untrusted id="...">` blocks with a fresh random id. (#18)
- Per-computer tokens are bound to ids; `tokens.json` is created with mode 0600 and `O_EXCL`, then updated atomically. Running relays reload it and disconnect connections whose tokens were replaced. (#17)

## [0.1.0] - 2026-11-18

### Added

- Initial MCP server: `cc_exec`, `cc_shell`, `cc_read`, `cc_write`, `cc_list`, `cc_peripherals`, `cc_log`, `cc_cancel`, `cc_ping`, `cc_computers`, `cc_network`, `cc_call`, `cc_batch`, `cc_push`, `cc_pull`.
- aiohttp relay with API and computer tokens, `bridge.lua` hosting the shell with terminal history and a `printError` log, and the `ccctl` command-line client.

[Unreleased]: https://github.com/slashracer5/computercraft-live-mcp/compare/1231769...main
[0.1.0]: https://github.com/slashracer5/computercraft-live-mcp/commit/1231769
