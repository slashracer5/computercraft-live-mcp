"""Per-operation text shared by MCP tools and ccctl."""

import json

from .cc_lua import parse_returned_json

HINTS = {
    "relay_unreachable": "The relay is not running. Start it with `computercraft-mcp-relay` (leave it running), "
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
}


def hint_for(res: dict) -> str | None:
    e = res.get("error")
    kind = e.get("kind") if isinstance(e, dict) else None
    return HINTS.get(kind) if isinstance(kind, str) else None


def _untrusted(text: str, source: str) -> str:
    """Single emission point for computer data; issue #18 will wrap it in markers."""
    return text


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
        lines.append("  " + _untrusted(f"{error.get('kind')}: {error.get('message')}", "error"))
        if error.get("traceback"):
            trace = _text(error["traceback"]).strip().replace("\n", "\n  ")
            lines.append("  " + _untrusted(trace, "traceback"))
    elif error:
        lines.append("  " + _untrusted(_text(error), "error"))
    if res.get("stderr"):
        lines.append("stderr:")
        stderr = res["stderr"]
        text = "\n".join("  " + _text(line) for line in stderr) if isinstance(stderr, list) else _text(stderr)
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
        lines.append("returns: " + _untrusted(text, "returns"))
    return lines + _output(res)


def _read(res: dict) -> list[str]:
    lines = []
    if res.get("eof") is False:
        lines.append(f"[more data: call again with offset={_number(res.get('next_offset'))}]")
    if res.get("data") is not None:
        lines.append(_untrusted(_text(res["data"]), "data"))
    return lines + _output(res)


def _write(res: dict) -> list[str]:
    lines = [f"remote size {_number(res.get('size'))} bytes"] if res.get("ok", True) else []
    return lines + _output(res)


def _list(res: dict) -> list[str]:
    entries = _records(res.get("entries"))
    width = max((len(_number(entry.get("size"))) for entry in entries if not entry.get("isDir")), default=1)
    lines = []
    for entry in entries:
        name = _untrusted(_text(entry.get("name", "?")), "entry.name")
        if entry.get("isDir"):
            lines.append(f"d {name}/")
        else:
            lines.append(f"  {_number(entry.get('size')):>{width}} {name}")
    return lines + _output(res)


def _network_lines(doc: dict) -> list[str]:
    devices = _records(doc.get("devices"))
    ship = []
    if "on_ship" in doc:
        ship.append("on_ship=" + _untrusted(_text(doc["on_ship"]), "on_ship"))
    if "ship_mass" in doc:
        ship.append(f"ship_mass={_number(doc['ship_mass'])}")
    lines = [f"{len(devices)} peripherals" + (f" ({', '.join(ship)})" if ship else "")]
    lines.append(
        "Names can change after rewiring or rebuilding (ship_* zone names derive from the bow): "
        "find peripherals by name prefix at run time, do not hard-code them."
    )
    for modem in _records(doc.get("modems")):
        lines.append("modem: " + _untrusted(", ".join(f"{k}={v}" for k, v in modem.items()), "modem"))
    groups: dict[tuple, list[dict]] = {}
    for device in devices:
        types = tuple(_text(t) for t in _items(device.get("types")))
        methods = tuple(sorted(_text(m) for m in _items(device.get("methods")))) if "methods" in device else None
        groups.setdefault((types, methods), []).append(device)
    for (types, methods), items in sorted(groups.items(), key=lambda kv: kv[0][0]):
        lines.append("")
        lines.append("type " + _untrusted("/".join(types) or "?", "peripheral.types") + f"  x{len(items)}")
        names = sorted(_text(device.get("name", "?")) for device in items)
        lines.append("  names: " + _untrusted(", ".join(names), "peripheral.names"))
        if methods is not None:
            text = _untrusted(", ".join(methods), "peripheral.methods") if methods else "(none)"
            lines.append("  methods: " + text)
        for device in items:
            if device.get("methods_error"):
                name = _untrusted(_text(device.get("name", "?")), "peripheral.name")
                error = _untrusted(_text(device["methods_error"]), "peripheral.methods_error")
                lines.append(f"  {name}: methods error: {error}")
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
        for error in errors:
            when = _untrusted(_text(error.get("when", "?")), "printError.when")
            text = _untrusted(_text(error.get("text", "")), "printError.text")
            lines.append(f"  #{_number(error.get('id'))} [{when}] {text}")
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
    return [", ".join(f"{key}=" + _untrusted(str(res.get(key)), f"ping.{key}") for key in keys)]


def _computers(res: dict) -> list[str]:
    if not res.get("ok", True) or res.get("error"):
        return []
    lines = []
    for computer in _records(res.get("computers")):
        fields = [f"{key}=" + _untrusted(str(computer.get(key)), f"computer.{key}") for key in ("id", "label", "host")]
        fields.append(f"connected_for_s={_number(computer.get('connected_for_s'))}")
        lines.append(", ".join(fields))
    return lines or ["No computers connected."]


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
            lines.append(f"{head} -> " + _untrusted(_text(result), "call.result"))
        elif result.get("ok"):
            values = _items(result.get("values"))
            text = _untrusted(", ".join(json.dumps(v) for v in values), "call.values") if values else "(no value)"
            lines.append(f"{head} -> {text}")
        else:
            lines.append(f"{head} -> ERROR " + _untrusted(_text(result.get("error")), "call.error"))
    return lines + _output(res)


def _push(res: dict) -> list[str]:
    lines = []
    if res.get("ok", True):
        lines.append(
            f"uploaded {_number(res.get('chars'))} chars in {_number(res.get('chunks'))} chunk(s); "
            f"remote size {_number(res.get('size'))} bytes"
        )
    return lines + _output(res)


def _pull(res: dict) -> list[str]:
    if not res.get("ok", True):
        return []
    return [f"saved {_number(res.get('bytes'))} bytes to {res.get('path')}"]


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
}


def format_result(op: str, res: dict, *, calls: list[dict] | None = None) -> str:
    """Format one result; calls supplies request labels for cc_call/cc_batch replies."""
    formatter = FORMATTERS.get(op, _generic)
    body = formatter(res, calls=calls) if op == "calls" else formatter(res)
    return "\n".join(_header(res) + body)
