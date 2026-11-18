"""Lua snippet builders for the higher-level MCP tools (cc_network, cc_call, cc_batch, cc_push).

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


# ---------------------------------------------------------------------------------- cc_push (atomic upload)
# An overwriting push writes "<target>.part" chunk by chunk, then swaps it into place with PUSH_FINALISE.
# `part` is nil for append=True, which writes straight to the target.
PUSH_PREFLIGHT = r"""
local target, part, need = %(target)s, %(part)s, %(need)d
local function done(t) return textutils.serialiseJSON(t) end
if fs.isReadOnly(target) then return done({ ok = false, reason = "read_only" }) end
if fs.isDir(target) then return done({ ok = false, reason = "is_directory" }) end
if part and fs.isDir(part) then return done({ ok = false, reason = "part_is_directory" }) end
local probe = target -- free space is per drive: ask the nearest folder that exists
for _ = 1, 64 do
  if probe == "" or probe == ".." or fs.exists(probe) then break end
  probe = fs.getDir(probe)
end
local okFree, free = pcall(fs.getFreeSpace, probe == ".." and "" or probe)
if not okFree then free = nil end
if type(free) == "number" then -- otherwise "unlimited"
  if part and fs.exists(part) then free = free + fs.getSize(part) end -- a stale .part gets overwritten
  if free < need then return done({ ok = false, reason = "no_space", free = free, need = need }) end
end
return done({ ok = true, free = free })
"""

PUSH_FINALISE = r"""
local target, part = %(target)s, %(part)s
local function done(t) return textutils.serialiseJSON(t) end
if fs.isDir(target) then return done({ ok = false, reason = "is_directory" }) end
if not fs.exists(part) or fs.isDir(part) then return done({ ok = false, reason = "part_missing" }) end
local existed = fs.exists(target)
if existed then
  local ok, err = pcall(fs.delete, target)
  if not ok then return done({ ok = false, reason = "delete_failed", message = tostring(err) }) end
end
local ok, err = pcall(fs.move, part, target)
if not ok then
  return done({ ok = false, reason = "move_failed", message = tostring(err), target_deleted = existed })
end
return done({ ok = true, size = fs.getSize(target) })
"""

PUSH_DISCARD = r"""
local part = %(part)s
if fs.exists(part) and not fs.isDir(part) then pcall(fs.delete, part) end
return textutils.serialiseJSON({ gone = not fs.exists(part) })
"""


def push_preflight_code(target: str, part: str | None, need: int) -> str:
    """Refuse read-only or directory targets, and uploads of `need` bytes the target's drive cannot hold."""
    return PUSH_PREFLIGHT % {
        "target": json_literal(target),
        "part": "nil" if part is None else json_literal(part),
        "need": need,
    }


def push_finalise_code(target: str, part: str) -> str:
    """Replace `target` with the finished `part` file: delete, then move, in one request."""
    return PUSH_FINALISE % {"target": json_literal(target), "part": json_literal(part)}


def push_discard_code(part: str) -> str:
    """Delete a leftover `part` file (never a directory)."""
    return PUSH_DISCARD % {"part": json_literal(part)}


def parse_returned_json(res: dict):
    """Pull the JSON document a snippet returned out of an `exec` reply (or None)."""
    rets = res.get("returns") or []
    if not isinstance(rets, list) or not rets:
        return None
    val = rets[0]
    for _ in range(2):  # exec serialises a Lua string as a JSON string; we returned JSON text inside it
        if isinstance(val, str):
            try:
                val = json.loads(val)
            except ValueError:
                return None
    return val if isinstance(val, (dict, list)) else None
