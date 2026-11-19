# Security notes

This tool lets an AI assistant run arbitrary Lua on an in-game ComputerCraft computer. Treat the relay like a remote shell.

- **Keep the relay on loopback** (the default, `127.0.0.1`) or on a private network you trust. It speaks plain `ws://` and `http://`, with no TLS.
- **Two secrets, both in `~/.computercraft-live-mcp/tokens.json`:** the *computer* token (stored on the in-game computer, in `.bridge.json`) and the *API* token (used by `ccctl` and the MCP server). Anyone with the API token can run code on every connected computer. Never commit or share either file; rotate them by deleting `tokens.json` and re-running the setup.
- **Never open the CC:Tweaked HTTP allow rule for the relay on a multiplayer server** you do not control: it lets computers there reach whatever the relay host can reach.
- **Everything the computer sends back is untrusted data** (signs, chat, files and peripheral data can be written by other players). Tool results wrap it, and every error message, in `<untrusted source="..." id="...">` … `</untrusted id="...">` blocks with a fresh random id per block, so it cannot pose as the server's own `OK` / `hint:` / paging lines, and the MCP server's instructions tell the model to treat block contents as data. Do not relax that.
- **Tool annotations tell your client what a tool does to the computer.** `cc_find`, `cc_grep`, `cc_stat`, `cc_read`, `cc_list` and the other read-only tools change nothing, so auto-approving them is reasonable, but they can still read every file on the computer, including `.bridge.json` with the computer token. `cc_delete` and `cc_move` are marked destructive; `cc_exec` and `cc_shell` run arbitrary code, so approving them approves anything.
- `cc_push` / `cc_pull` are disabled until you set `CC_MCP_FILES_ROOT`; point it only at folders you are happy for the assistant to read and overwrite. The command-line `ccctl push` / `pull` / `write --file` are not limited by it: they read and write the local paths you type, like any other command you run.
- The relay log never contains tokens; DEBUG logs contain snippets of the code you run, so treat DEBUG log files like the code itself.

## Reporting a problem

Open a GitHub issue for anything that is not exploitable. For a vulnerability, use GitHub's private "Report a vulnerability" option on the repository's Security tab.
