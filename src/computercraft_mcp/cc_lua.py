"""Lua snippet builders for the higher-level MCP tools (cc_network, cc_call, cc_batch).

They run through the ordinary `exec` op, so the in-game bridge.lua needs no change. Every snippet
returns ONE JSON string (textutils.serialiseJSON) so the Python side can parse it back.
"""

import json


def long_string(s: str) -> str:
    """A Lua long-bracket literal that cannot be terminated by anything inside `s`."""
    level = 1  # >=1 so text ending in "]" cannot merge with the closing bracket
    while f"]{'=' * level}]" in s:
        level += 1
    eq = "=" * level
    return f"[{eq}[{s}]{eq}]"


def json_literal(obj) -> str:
    """Lua expression that evaluates to `obj` (decoded from JSON inside the computer).

    JSON null becomes nil and array holes are therefore possible: callers pass the length separately.
    """
    return f"textutils.unserialiseJSON({long_string(json.dumps(obj, ensure_ascii=True))})"


# ---------------------------------------------------------------------------------- cc_network
NETWORK = r"""
local want_methods = %(methods)s
local names = peripheral.getNames()
table.sort(names)
local out = { devices = {}, modems = {}, local_names = {} }
local ok_ship, on_ship = pcall(function() return sublevel.isInPlotGrid() end)
if ok_ship then out.on_ship = on_ship end
local ok_mass, mass = pcall(function() return sublevel.getMass() end)
if ok_mass then out.ship_mass = mass end
for _, n in ipairs(names) do
  local okT, types = pcall(function() return { peripheral.getType(n) } end)
  local entry = { name = n, types = okT and types or { "?" } }
  if want_methods then
    local okM, m = pcall(peripheral.getMethods, n)
    if okM and m then table.sort(m); entry.methods = m else entry.methods_error = tostring(m) end
  end
  out.devices[#out.devices + 1] = entry
  for _, t in ipairs(entry.types) do
    if t == "modem" then
      local info = { side = n }
      local ok1, v1 = pcall(peripheral.call, n, "getNameLocal"); if ok1 then info.local_name = v1 end
      local ok2, v2 = pcall(peripheral.call, n, "isWireless"); if ok2 then info.wireless = v2 end
      local ok3, v3 = pcall(peripheral.call, n, "getNamesRemote"); if ok3 then info.remote_count = #v3 end
      out.modems[#out.modems + 1] = info
    end
  end
end
return textutils.serialiseJSON(out)
"""


def network_code(methods: bool) -> str:
    return NETWORK % {"methods": "true" if methods else "false"}


# ---------------------------------------------------------------------------------- cc_call / cc_batch
CALLS = r"""
local calls = %(calls)s
local n = %(n)d
local parallel_mode = %(parallel)s
local results = {}
local t0 = os.epoch("utc")
local function pack_result(res)
  local out = {}
  for i = 1, res.n - 1 do out[i] = res[i + 1] end
  return out
end
local function run(i)
  local c = calls[i]
  local nargs = tonumber(c.n) or (c.args and #c.args) or 0
  local res = table.pack(pcall(peripheral.call, c.p, c.m, table.unpack(c.args or {}, 1, nargs)))
  if res[1] then
    results[i] = { ok = true, values = pack_result(res) }
  else
    results[i] = { ok = false, error = tostring(res[2]) }
  end
end
if parallel_mode and n > 1 then
  local jobs = {}
  for i = 1, n do jobs[i] = function() run(i) end end
  parallel.waitForAll(table.unpack(jobs, 1, n))
else
  for i = 1, n do run(i) end
end
return textutils.serialiseJSON({ results = results, elapsed_ms = os.epoch("utc") - t0, n = n })
"""


def calls_code(calls: list[dict], parallel: bool) -> str:
    """calls: [{"peripheral": str, "method": str, "args": [...]}]"""
    packed = []
    for c in calls:
        args = list(c.get("args") or [])
        packed.append({"p": c["peripheral"], "m": c["method"], "args": args, "n": len(args)})
    return CALLS % {"calls": json_literal(packed), "n": len(packed), "parallel": "true" if parallel else "false"}


def parse_returned_json(res: dict):
    """Pull the JSON document a snippet returned out of an `exec` reply (or None)."""
    rets = res.get("returns") or []
    if not rets:
        return None
    val = rets[0]
    for _ in range(2):  # exec serialises a Lua string as a JSON string; we returned JSON text inside it
        if isinstance(val, str):
            try:
                val = json.loads(val)
            except ValueError:
                return None
    return val if isinstance(val, (dict, list)) else None


# ---------------------------------------------------------------------------------- formatting
def format_network(doc: dict) -> str:
    devices = doc.get("devices") or []
    lines = []
    ship = []
    if "on_ship" in doc:
        ship.append(f"on_ship={doc['on_ship']}")
    if "ship_mass" in doc:
        ship.append(f"ship_mass={doc['ship_mass']}")
    lines.append(f"{len(devices)} peripherals" + (f" ({', '.join(ship)})" if ship else ""))
    for m in doc.get("modems") or []:
        lines.append("modem: " + ", ".join(f"{k}={v}" for k, v in m.items()))
    groups: dict[tuple, list[dict]] = {}
    for d in devices:
        key = (tuple(d.get("types") or ()), tuple(d.get("methods") or ()) if "methods" in d else None)
        groups.setdefault(key, []).append(d)
    for (types, methods), items in sorted(groups.items(), key=lambda kv: kv[0][0]):
        lines.append("")
        lines.append(f"type {'/'.join(types) or '?'}  x{len(items)}")
        lines.append("  names: " + ", ".join(d["name"] for d in items))
        if methods is not None:
            lines.append("  methods: " + (", ".join(methods) or "(none)"))
        for d in items:
            if d.get("methods_error"):
                lines.append(f"  {d['name']}: methods error: {d['methods_error']}")
    lines.append("")
    lines.append(
        "Names can change after rewiring or rebuilding (ship_* zone names derive from the bow): "
        "find peripherals by name prefix at run time, do not hard-code them."
    )
    return "\n".join(lines)


def format_calls(doc: dict, calls: list[dict]) -> str:
    lines = [f"{doc.get('n', len(calls))} call(s) in {doc.get('elapsed_ms', '?')} ms"]
    for call, res in zip(calls, doc.get("results") or [], strict=False):
        head = f"{call['peripheral']}.{call['method']}({', '.join(json.dumps(a) for a in call.get('args') or [])})"
        if res.get("ok"):
            vals = res.get("values") or []
            lines.append(f"{head} -> " + (", ".join(json.dumps(v) for v in vals) if vals else "(no value)"))
        else:
            lines.append(f"{head} -> ERROR {res.get('error')}")
    return "\n".join(lines)
