"""Per-operation text shared by MCP tools and ccctl."""

import json
import re
import secrets
from datetime import datetime, timezone

from .cc_lua import parse_returned_json

HINTS = {
    "relay_unreachable": "The relay is not running. Start it with `computercraft-live-mcp-relay` (leave it running), "
    "then retry. The in-game computer reconnects by itself within seconds.",
    "no_target": "Several computers are connected: repeat the call with computer=<id> (cc_computers lists them).",
    "not_connected": "That computer is offline (world closed, chunk unloaded, or bridge stopped). "
    "cc_computers shows who is connected.",
    "disconnected": "The computer dropped mid-request (chunk unload, reboot or world exit). "
    "Check cc_computers, then retry.",
    "busy": "A previous request is still running on that computer. Call cc_cancel, then retry. If this keeps happening "
    "the game is probably paused or unfocused: yielding peripheral calls only progress while the game is ticking.",
    "relay_timeout": "The computer did not answer even its own timeout timer, which almost always means Minecraft "
    "is paused/unfocused or the computer's chunk is not loaded. A cancel was queued for it. Focus the game and retry; "
    "use cc_ping to see if it answers at all.",
    "timeout": "The request ran past its timeout and was abandoned on the computer. Raise `timeout`, split the work, "
    "or (for peripheral setters) make sure the game is focused and unpaused.",
    "no_space": "The computer's drive has too little free space for this upload (an overwrite needs room for the new "
    "file next to the old one until it is swapped in). Free space (cc_list shows sizes, cc_ping free bytes) or push "
    "to another drive.",
    "read_only": "That path is read-only (rom/ or a read-only mount): choose a writable path.",
    "is_directory": "That path is a directory: give the full file path, e.g. folder/name.lua.",
    "replace_failed": "The upload arrived but swapping it into place failed; the `sent N of M chars` line says what "
    "is on the computer now. Check with cc_list before retrying.",
    "not_found": "Nothing exists at that path (paths start at the computer's root). cc_list or cc_find show what is "
    "there.",
    "exists": "The destination already exists and CC never overwrites it: choose another path, or delete the existing "
    "one first if that is really intended.",
    "not_empty": "cc_delete removes a non-empty directory only with recursive=True, which deletes everything inside: "
    "check its contents with cc_list first.",
}

# Error kinds printed as a server line ("  error: runtime"); any other kind goes inside the error block, so a
# reply cannot invent one. Add new kinds here. Messages are always wrapped: the formatter only sees a dict, and a
# computer's reply can claim a relay or client kind as easily as its own.
KNOWN_KINDS = frozenset(
    "syntax runtime exit timeout terminated cancelled internal busy disconnected bad_request".split()  # bridge
    + "relay_timeout too_large unauthorized no_target not_connected".split()  # relay
    + "relay_unreachable local_file".split()  # client
    + "no_space read_only is_directory replace_failed".split()  # cc_push checks
    + "not_found exists not_empty".split()  # file-system tools
)


def hint_for(res: dict) -> str | None:
    e = res.get("error")
    kind = e.get("kind") if isinstance(e, dict) else None
    return HINTS.get(kind) if isinstance(kind, str) else None


def _new_id() -> str:
    return secrets.token_hex(4)


def _untrusted(text: str, source: str, *, inline: bool = False) -> str:
    """Single emission point for computer data: wrap it verbatim in markers with a fresh random id.

    The id never occurs in the content, so the content cannot contain the closing marker. `source` is a fixed
    token from this module, never variable text. inline=True keeps a one-line value on its server line."""
    if not re.fullmatch(r"[a-zA-Z_]+", source):
        raise ValueError("source must be a fixed token")
    block_id = _new_id()
    while block_id in text:
        block_id = _new_id()
    separator = "" if inline and "\n" not in text and "\r" not in text else "\n"
    return f'<untrusted source="{source}" id="{block_id}">{separator}{text}{separator}</untrusted id="{block_id}">'


def _text(value) -> str:
    return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)


def _items(value) -> list:
    # CC may serialise an empty Lua array as {}.
    return value if isinstance(value, list) else []


def _records(value) -> list[dict]:
    return [item for item in _items(value) if isinstance(item, dict)]


def _number(value) -> str:
    return str(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else "?"


def _header(res: dict) -> list[str]:
    lines = ["OK" if res.get("ok", True) else "ERROR"]
    if "duration" in res:
        duration = res["duration"]
        lines[0] += f" [{duration:.2f}s]" if isinstance(duration, (int, float)) else " [?s]"
    error = res.get("error")
    hint = hint_for(res)
    if hint:
        lines.append("  hint: " + hint)
    if isinstance(error, dict):
        kind = error.get("kind")
        parts = []
        if isinstance(kind, str) and kind in KNOWN_KINDS:
            lines.append("  error: " + kind)
        else:
            parts.append("kind: " + _text(kind))
        if error.get("message") is not None:
            parts.append(_text(error["message"]))
        if error.get("traceback"):
            parts.append(_text(error["traceback"]).strip())
        if parts:
            lines.append(_untrusted("\n".join(parts), "error"))
    elif error:
        lines.append(_untrusted(_text(error), "error"))
    if res.get("stderr"):
        lines.append("stderr:")
        stderr = res["stderr"]
        text = "\n".join(_text(line) for line in stderr) if isinstance(stderr, list) else _text(stderr)
        lines.append(_untrusted(text, "stderr"))
    return lines


def _output(res: dict) -> list[str]:
    if not res.get("output"):
        return []
    return [
        "output:" + (" (truncated, tail kept)" if res.get("truncated") else ""),
        _untrusted(_text(res["output"]), "output"),
    ]


def _exec(res: dict) -> list[str]:
    lines = []
    if res.get("returns"):
        values = res["returns"]
        text = ", ".join(_text(value) for value in values) if isinstance(values, list) else _text(values)
        lines.append("returns: " + _untrusted(text, "returns", inline=True))
    return lines + _output(res)


def _read(res: dict) -> list[str]:
    lines = []
    if res.get("eof") is False:
        lines.append(f"[more data: call again with offset={_number(res.get('next_offset'))}]")
    if res.get("data") is not None:
        lines.append(_untrusted(_text(res["data"]), "file"))
    return lines + _output(res)


def _write(res: dict) -> list[str]:
    lines = [f"remote size {_number(res.get('size'))} bytes"] if res.get("ok", True) else []
    return lines + _output(res)


def _entry_lines(entries: list[dict], key: str) -> list[str]:
    """`d name/` for directories, right-aligned byte size then name for files (cc_list, cc_find)."""
    width = max((len(_number(entry.get("size"))) for entry in entries if not entry.get("isDir")), default=1)
    lines = []
    for entry in entries:
        name = _text(entry.get(key, "?"))
        if entry.get("isDir"):
            lines.append(f"d {name}/")
        else:
            lines.append(f"  {_number(entry.get('size')):>{width}} {name}")
    return lines


def _list(res: dict) -> list[str]:
    lines = _entry_lines(_records(res.get("entries")), "name")
    return ([_untrusted("\n".join(lines), "listing")] if lines else []) + _output(res)


def _network_lines(doc: dict) -> list[str]:
    devices = _records(doc.get("devices"))
    ship = []
    if "on_ship" in doc:
        on_ship = doc["on_ship"]
        text = str(on_ship).lower() if isinstance(on_ship, bool) else _untrusted(_text(on_ship), "on_ship", inline=True)
        ship.append("on_ship=" + text)
    if "ship_mass" in doc:
        ship.append(f"ship_mass={_number(doc['ship_mass'])}")
    lines = [f"{len(devices)} peripherals" + (f" ({', '.join(ship)})" if ship else "")]
    lines.append(
        "Names can change after rewiring or rebuilding (ship_* zone names derive from the bow): "
        "find peripherals by name prefix at run time, do not hard-code them."
    )
    data = []
    for modem in _records(doc.get("modems")):
        data.append("modem: " + ", ".join(f"{k}={v}" for k, v in modem.items()))
    groups: dict[tuple, list[dict]] = {}
    for device in devices:
        types = tuple(_text(t) for t in _items(device.get("types")))
        methods = tuple(sorted(_text(m) for m in _items(device.get("methods")))) if "methods" in device else None
        groups.setdefault((types, methods), []).append(device)
    for (types, methods), items in sorted(groups.items(), key=lambda kv: kv[0][0]):
        if data:
            data.append("")
        data.append("type " + ("/".join(types) or "?") + f"  x{len(items)}")
        names = sorted(_text(device.get("name", "?")) for device in items)
        data.append("  names: " + ", ".join(names))
        if methods is not None:
            data.append("  methods: " + (", ".join(methods) if methods else "(none)"))
        for device in items:
            if device.get("methods_error"):
                name = _text(device.get("name", "?"))
                error = _text(device["methods_error"])
                data.append(f"  {name}: methods error: {error}")
    if data:
        lines.append(_untrusted("\n".join(data), "peripherals"))
    return lines


def _peripherals(res: dict) -> list[str]:
    lines = _network_lines({"devices": res["peripherals"]}) if "peripherals" in res else []
    return lines + _output(res)


def _log(res: dict) -> list[str]:
    lines = []
    if any(key in res for key in ("lines", "errors", "last_error_id")):
        lines.append(f"last_error_id: {_number(res.get('last_error_id', 0))}")
    terminal = _items(res.get("lines"))
    if terminal:
        lines.append("terminal (! = red text):")
        red = _items(res.get("red"))
        text = "\n".join(("! " if i in red else "  ") + _text(line) for i, line in enumerate(terminal, 1))
        lines.append(_untrusted(text, "terminal"))
    errors = _records(res.get("errors"))
    if errors:
        lines.append("printError log:")
        entries = []
        for error in errors:
            when = _text(error.get("when", "?"))
            text = _text(error.get("text", ""))
            entries.append(f"  #{_number(error.get('id'))} [{when}] {text}")
        lines.append(_untrusted("\n".join(entries), "printError"))
    return lines


def _cancel(res: dict) -> list[str]:
    if not res.get("ok", True):
        return []
    cancelled = res.get("cancelled")
    return ["cancelled: " + (str(cancelled).lower() if isinstance(cancelled, bool) else "?")]


def _ping(res: dict) -> list[str]:
    if not res.get("ok", True):
        return []
    keys = ("id", "label", "clock", "day", "host", "free")
    return [_untrusted(", ".join(f"{key}={res.get(key)}" for key in keys), "ping", inline=True)]


def _computers(res: dict) -> list[str]:
    if not res.get("ok", True) or res.get("error"):
        return []
    lines = []
    for computer in _records(res.get("computers")):
        fields = [f"{key}={computer.get(key)}" for key in ("id", "label", "host")]
        fields.append(f"connected_for_s={_number(computer.get('connected_for_s'))}")
        lines.append(", ".join(fields))
    return [_untrusted("\n".join(lines), "computers")] if lines else ["No computers connected."]


def _network(res: dict) -> list[str]:
    doc = parse_returned_json(res) if res.get("ok", True) else None
    if not isinstance(doc, dict) or "devices" not in doc:
        return _exec(res)
    return _network_lines(doc) + _output(res)


def _calls(res: dict, *, calls: list[dict] | None = None) -> list[str]:
    doc = parse_returned_json(res) if res.get("ok", True) else None
    if not isinstance(doc, dict) or "results" not in doc:
        return _exec(res)
    calls = calls or []
    lines = [f"{_number(doc.get('n', len(calls)))} call(s) in {_number(doc.get('elapsed_ms'))} ms"]
    for i, result in enumerate(_items(doc.get("results"))):
        call = calls[i] if i < len(calls) and isinstance(calls[i], dict) else {}
        args = ", ".join(json.dumps(a) for a in _items(call.get("args")))
        head = f"{call.get('peripheral', '?')}.{call.get('method', '?')}({args})"
        if not isinstance(result, dict):
            lines.append(f"{head} -> " + _untrusted(_text(result), "call"))
        elif result.get("ok"):
            values = _items(result.get("values"))
            text = _untrusted(", ".join(json.dumps(v) for v in values), "call", inline=True) if values else "(no value)"
            lines.append(f"{head} -> {text}")
        else:
            lines.append(f"{head} -> ERROR " + _untrusted(_text(result.get("error")), "call_error", inline=True))
    return lines + _output(res)


# remote_state of a failed push (set by bridge_client.push_text): what is on the computer now
PUSH_STATES = {
    "unchanged": "the remote file is unchanged",
    "append": "append is not atomic: the remote file may end with part of the upload",
    "unknown": "the final swap was not confirmed: check the remote file before retrying",
    "part_kept": "the old remote file was deleted and the complete upload is in the .part file next to it",
}


def _push(res: dict) -> list[str]:
    lines = []
    if res.get("ok", True):
        lines.append(
            f"uploaded {_number(res.get('chars'))} chars in {_number(res.get('chunks'))} chunk(s); "
            f"remote size {_number(res.get('size'))} bytes"
        )
    elif "sent_chars" in res:
        line = (
            f"sent {_number(res.get('sent_chars'))} of {_number(res.get('chars'))} chars "
            f"in {_number(res.get('chunks'))} chunk(s)"
        )
        state = res.get("remote_state")
        if isinstance(state, str) and state in PUSH_STATES:
            line += "; " + PUSH_STATES[state]
        lines.append(line)
        if res.get("part_left") is True:
            lines.append("the temporary .part file may remain; the next push to this path overwrites it")
    return lines + _output(res)


def _pull(res: dict) -> list[str]:
    if not res.get("ok", True):
        return []
    return [f"saved {_number(res.get('bytes'))} bytes to {res.get('path')}"]


# ---------------------------------------------------------------------------------- file-system tools
# Results built by bridge_client.fs_*: every field came from the computer, so only booleans, numbers and fixed
# words reach server lines; paths, drive names and matched lines go inside markers.
def _is_number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _count(value) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def _flag(value) -> str:
    return str(value).lower() if isinstance(value, bool) else "?"


def _timestamp(value) -> str | None:
    """CC's fs.attributes times (milliseconds since the epoch) as UTC; None when missing or nonsense."""
    if not _is_number(value) or value <= 0:
        return None
    try:
        return datetime.fromtimestamp(value / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    except (OverflowError, OSError, ValueError):
        return None


def _find(res: dict) -> list[str]:
    if not res.get("ok", True):
        return _output(res)
    entries = _records(res.get("entries"))
    total = _count(res.get("total"))
    line = f"{len(entries)} matching path(s)"
    if total is not None and total > len(entries):
        why = "not all fit in one reply" if res.get("cut") is True else f"max_results={_number(res.get('limit'))}"
        line = f"{len(entries)} of {total} matching paths shown ({why}): narrow the pattern to see the rest"
    lines = [line]
    if entries:
        lines.append(_untrusted("\n".join(_entry_lines(entries, "path")), "paths"))
    return lines + _output(res)


def _grep(res: dict) -> list[str]:
    if not res.get("ok", True):
        return _output(res)
    matches = _records(res.get("matches"))
    notes = [f"{len(matches)} match(es) in {_number(res.get('files'))} file(s)"]
    if res.get("rom_skipped") is True:
        notes.append("rom/ skipped (include_rom=True searches it)")
    for key, what in (("binary", "binary file(s) skipped"), ("unreadable", "unreadable path(s) skipped")):
        if _count(res.get(key)):
            notes.append(f"{res[key]} {what}")
    if res.get("stopped") == "limit":
        notes.append(f"stopped at max_matches={_number(res.get('limit'))}: there may be more")
    elif res.get("stopped") == "time":
        notes.append("stopped early to answer within the timeout: raise timeout or search a narrower path")
    if res.get("cut") is True:
        notes.append("not all matches fit in one reply: narrow the search")
    lines = ["; ".join(notes)]
    if matches:
        found = (f"{_text(m.get('path', '?'))}:{_number(m.get('line'))}: {_text(m.get('text', ''))}" for m in matches)
        lines.append(_untrusted("\n".join(found), "matches"))
    return lines + _output(res)


def _stat(res: dict) -> list[str]:
    if not res.get("ok", True):
        return _output(res)
    lines = ["exists: " + _flag(res.get("exists"))]
    if res.get("exists") is not True:
        return lines + _output(res)
    if res.get("isDir") is True:
        entries = _count(res.get("entries"))
        lines.append("type: directory" + (f", {entries} item(s)" if entries is not None else ""))
    else:
        lines.append(f"type: file, {_number(res.get('size'))} bytes" if res.get("isDir") is False else "type: ?")
    lines.append("read-only: " + _flag(res.get("isReadOnly")))
    if res.get("drive") is not None:
        lines.append("drive: " + _untrusted(_text(res["drive"]), "drive", inline=True))
    if _is_number(res.get("capacity")):
        lines.append(f"capacity: {_number(res['capacity'])} bytes")
    free = res.get("free")
    if free == "unlimited" or _is_number(free):
        lines.append(f"free: {_number(free)} bytes" if _is_number(free) else "free: unlimited")
    for key in ("created", "modified"):
        when = _timestamp(res.get(key))
        if when:
            lines.append(f"{key}: {when}")
    return lines + _output(res)


FS_COUNTS = (("files", "file(s)"), ("dirs", "subdirectory(s)"), ("bytes", "bytes"))  # what a recursive delete removed


def _what(res: dict) -> str:
    if res.get("isDir") is True:
        if any(key in res for key, _ in FS_COUNTS):
            return "directory (" + ", ".join(f"{_number(res.get(key))} {what}" for key, what in FS_COUNTS) + ")"
        return "directory"
    return f"file ({_number(res.get('size'))} bytes)" if res.get("isDir") is False else "?"


def _delete(res: dict) -> list[str]:
    if not res.get("ok", True):
        return _output(res)
    return [f"deleted {_what(res)}: " + _untrusted(_text(res.get("path", "?")), "path", inline=True)] + _output(res)


def _transfer(res: dict, verb: str) -> list[str]:
    if not res.get("ok", True):
        return _output(res)
    paths = f"{_text(res.get('from', '?'))} -> {_text(res.get('to', '?'))}"
    return [f"{verb} {_what(res)}: " + _untrusted(paths, "path", inline=True)] + _output(res)


def _move(res: dict) -> list[str]:
    return _transfer(res, "moved")


def _copy(res: dict) -> list[str]:
    return _transfer(res, "copied")


def _mkdir(res: dict) -> list[str]:
    if not res.get("ok", True):
        return _output(res)
    existed = res.get("existed")
    what = "directory already exists" if existed is True else "created directory" if existed is False else "?"
    return [f"{what}: " + _untrusted(_text(res.get("path", "?")), "path", inline=True)] + _output(res)


def _generic(res: dict) -> list[str]:
    data = {key: value for key, value in res.items() if key not in {"ok", "error", "stderr", "duration"}}
    return [_untrusted(json.dumps(data, indent=2, ensure_ascii=False), "result")] if data else []


FORMATTERS = {
    "exec": _exec,
    "shell": _exec,
    "read": _read,
    "write": _write,
    "list": _list,
    "peripherals": _peripherals,
    "log": _log,
    "cancel": _cancel,
    "ping": _ping,
    "computers": _computers,
    "network": _network,
    "calls": _calls,
    "push": _push,
    "pull": _pull,
    "find": _find,
    "grep": _grep,
    "stat": _stat,
    "delete": _delete,
    "move": _move,
    "copy": _copy,
    "mkdir": _mkdir,
}


def format_result(op: str, res: dict, *, calls: list[dict] | None = None) -> str:
    """Format one result; calls supplies request labels for cc_call/cc_batch replies."""
    formatter = FORMATTERS.get(op, _generic)
    header = _header(res)
    body = formatter(res, calls=calls) if op == "calls" else formatter(res)
    return "\n".join(header + body)
