"""Lua snippet builders for the higher-level MCP tools (cc_network, cc_call, cc_batch, cc_push, the file-system tools).

They run through the ordinary `exec` op, so the in-game bridge.lua needs no change. Every snippet
returns ONE JSON string (textutils.serialiseJSON) so the Python side can parse it back.
Mod-specific probes, output and instructions live in opt-in profiles (PROFILES, CC_MCP_PROFILE).
"""

import json
import sys
from collections.abc import Iterable
from dataclasses import dataclass

from .config import get_config
from .limits import FS_REPLY_LIMIT


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


# ---------------------------------------------------------------------------------- mod profiles
@dataclass(frozen=True)
class Profile:
    """Opt-in, mod-specific extras, enabled by name in CC_MCP_PROFILE. Without a profile the tools, their
    output and the server instructions stay independent of any mod."""

    name: str
    network_probe: str = ""  # Lua run at the end of cc_network (in its own block); may set fields on `out`
    network_fields: tuple[str, ...] = ()  # those fields, shown on cc_network's first line when present
    network_notes: tuple[str, ...] = ()  # lines added to the end of cc_network's output
    instructions: str = ""  # appended to the MCP server instructions


SABLE = Profile(
    name="sable",
    network_probe="""
local ok_ship, on_ship = pcall(function() return sublevel.isInPlotGrid() end)
if ok_ship then out.on_ship = on_ship end
local ok_mass, mass = pcall(function() return sublevel.getMass() end)
if ok_mass then out.ship_mass = mass end
""",
    network_fields=("on_ship", "ship_mass"),
    network_notes=("ship_* zone names derive from the bow, so rebuilding the ship can rename them.",),
    instructions="""\
Profile "sable" (Sable ships with Create Avionics peripherals):
- cc_network also reports on_ship (whether this computer is on a Sable ship) and ship_mass (the
  ship's mass as Sable reports it); each is left out when the computer has no `sublevel` API.
- `ship_*` peripheral zone names derive from the bow: find them by prefix at run time.
- Do not trust the documentation for actuator signs or frames: verify them with a one-variable
  physical test and a sensor before relying on them.
""",
)

PROFILES = {p.name: p for p in (SABLE,)}
_warned: set[str] = set()


def active_profiles() -> tuple[Profile, ...]:
    """The profiles named in CC_MCP_PROFILE, in order. Unknown names are skipped with a warning on stderr."""
    found = []
    for name in get_config().profiles:
        if name in PROFILES:
            found.append(PROFILES[name])
        elif name not in _warned:
            _warned.add(name)
            known = ", ".join(sorted(PROFILES))
            print(
                f"computercraft-live-mcp: ignoring unknown CC_MCP_PROFILE entry {name!r} (known: {known})",
                file=sys.stderr,
            )
    return tuple(found)


def profile_instructions(profiles: Iterable[Profile] | None = None) -> str:
    """Server instructions added by `profiles` (default: those enabled), or "" for none."""
    profiles = active_profiles() if profiles is None else profiles
    return "".join("\n" + p.instructions for p in profiles if p.instructions)


def network_extras(doc: dict, profiles: Iterable[Profile] | None = None) -> tuple[list[tuple[str, object]], list[str]]:
    """What `profiles` (default: those enabled) add to cc_network's output: the (field, value) pairs present
    in `doc` for its first line, and note lines for its end."""
    profiles = active_profiles() if profiles is None else profiles
    fields: list[tuple[str, object]] = []
    notes: list[str] = []
    for p in profiles:
        fields += [(key, doc[key]) for key in p.network_fields if key in doc]
        notes += p.network_notes
    return fields, notes


# ---------------------------------------------------------------------------------- cc_network
NETWORK = r"""
local want_methods = %(methods)s
local names = peripheral.getNames()
table.sort(names)
local out = { devices = {}, modems = {}, local_names = {} }
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
%(probes)sreturn textutils.serialiseJSON(out)
"""


def network_code(methods: bool, profiles: Iterable[Profile] | None = None) -> str:
    """The cc_network snippet plus the probes of `profiles` (default: those enabled in CC_MCP_PROFILE)."""
    profiles = active_profiles() if profiles is None else profiles
    probes = "".join(f"do\n{p.network_probe.strip()}\nend\n" for p in profiles if p.network_probe)
    return NETWORK % {"methods": "true" if methods else "false", "probes": probes}


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


# ---------------------------------------------------------------------------------- file-system tools
# cc_find / cc_grep / cc_stat (read-only) and cc_delete / cc_move / cc_copy / cc_mkdir. Each snippet starts with
# FS_PRELUDE and answers { ok = true, ... } or { ok = false, reason = ... }; bridge_client turns reasons into errors.
#   done(t, list)  encodes t as UTF-8 JSON, or byte-wise with t.bytewise set when some text is not valid UTF-8
#                  (parse_text_json decodes it back), and drops the tail of `list` (setting t.cut) until the reply
#                  fits one bridge message (REPLY_LIMIT = limits.FS_REPLY_LIMIT): the bridge cannot shorten return
#                  values itself.
#   tick()         yields every 200 ms, so a long walk neither trips CC's "too long without yielding" abort nor
#                  holds up cc_cancel and the request's timeout timer.
#   outside(p)     true for the root directory and anything above it.
#   read_only(p)   whether p (existing or not) is on a read-only mount: fs.isReadOnly of the root folder of the
#                  mount holding p's nearest existing parent. CC's read-only paths are whole mounts (rom/, treasure
#                  disks), and CraftOS-PC's fs.isReadOnly answers true for writable folders made by fs.makeDir("a/b")
#                  and for new paths beside them.
# Placeholders are %-format fields; the Lua code itself must not contain a bare percent sign.
_FS_PRELUDE = r"""
local REPLY_LIMIT = %(reply_limit)d
local function encode(t)
  t.bytewise = nil
  local ok, s = pcall(textutils.serialiseJSON, t, { unicode_strings = true })
  if ok then return s end
  t.bytewise = true
  return textutils.serialiseJSON(t)
end
local function done(t, list)
  local s = encode(t)
  while list and #list > 0 do
    local size = #textutils.serialiseJSON(textutils.serialise(s)) -- as the bridge will send it
    if size <= REPLY_LIMIT then break end
    local keep = math.min(#list - 1, math.floor(#list * REPLY_LIMIT / size * 0.95))
    for i = #list, keep + 1, -1 do list[i] = nil end
    t.cut = true
    s = encode(t)
  end
  return s
end
local last_yield = os.epoch("utc")
local function tick()
  if os.epoch("utc") - last_yield >= 200 then
    os.queueEvent("cc_mcp_yield")
    os.pullEvent("cc_mcp_yield")
    last_yield = os.epoch("utc")
  end
end
local function outside(p)
  local n = fs.combine("", p)
  return n == "" or n == ".." or n:sub(1, 3) == "../"
end
local function read_only(p)
  p = fs.combine("", p)
  while p ~= "" and not fs.exists(p) do p = fs.getDir(p) end
  local drive = fs.getDrive(p)
  while p ~= "" and fs.getDrive(fs.getDir(p)) == drive do p = fs.getDir(p) end
  return fs.isReadOnly(p)
end
"""
FS_PRELUDE = _FS_PRELUDE % {"reply_limit": FS_REPLY_LIMIT}

FIND = r"""
local pattern, limit = %(pattern)s, %(limit)d
local ok, found = pcall(fs.find, pattern)
if not ok then return done({ ok = false, reason = "failed", message = tostring(found) }) end
table.sort(found)
local out = { ok = true, total = #found, limit = limit, entries = {} }
for i = 1, math.min(#found, limit) do
  local p = found[i]
  local entry = { path = p, isDir = fs.isDir(p) }
  if not entry.isDir then
    local okS, size = pcall(fs.getSize, p)
    if okS then entry.size = size end
  end
  out.entries[i] = entry
  tick()
end
return done(out, out.entries)
"""

GREP = r"""
local pattern, root, plain, limit, include_rom = %(pattern)s, %(root)s, %(plain)s, %(limit)d, %(include_rom)s
local deadline = os.epoch("utc") + %(budget_ms)d
local WIDTH = 200
if not plain then -- most malformed patterns fail on any subject; the rest are caught line by line below
  local ok, err = pcall(string.find, "", pattern)
  if not ok then return done({ ok = false, reason = "bad_pattern", message = tostring(err) }) end
end
if not fs.exists(root) then return done({ ok = false, reason = "not_found" }) end
root = fs.combine("", root)
local skip_rom = not include_rom and root ~= "rom" and root:sub(1, 4) ~= "rom/"
local out = { ok = true, matches = {}, files = 0, binary = 0, unreadable = 0, limit = limit }
local stop, bad_pattern
local function clip(line, s) -- long lines: WIDTH bytes around the match, cut on character boundaries
  if #line <= WIDTH then return line end
  local from = math.max(1, math.min(s - 60, #line - WIDTH + 1))
  local to = from + WIDTH - 1
  local text = line:sub(from, to)
  if from > 1 then text = "..." .. text:gsub("^[\128-\191]+", "") end
  if to < #line then text = text:gsub("[\192-\255][\128-\191]*$", "") .. "..." end
  return text
end
local function search(path)
  tick()
  if os.epoch("utc") >= deadline then stop = "time" return end
  local h = fs.open(path, "rb")
  if not h then out.unreadable = out.unreadable + 1 return end
  local data = h.readAll() or ""
  h.close()
  out.files = out.files + 1
  if data:sub(1, 1024):find("\0", 1, true) then out.binary = out.binary + 1 return end
  local pos, n, since, len = 1, 0, 0, #data
  while pos <= len do
    local e = data:find("\n", pos, true) or len + 1
    local line = data:sub(pos, e - 1)
    pos, n, since = e + 1, n + 1, since + 1
    if line:sub(-1) == "\r" then line = line:sub(1, -2) end
    local ok, s = true, nil
    if plain then s = line:find(pattern, 1, true) else ok, s = pcall(string.find, line, pattern) end
    if not ok then stop, bad_pattern = "bad_pattern", tostring(s) return end
    if s then
      out.matches[#out.matches + 1] = { path = path, line = n, text = clip(line, s) }
      if #out.matches >= limit then stop = "limit" return end
    end
    if since >= 500 then
      since = 0
      tick()
      if os.epoch("utc") >= deadline then stop = "time" return end
    end
  end
end
local function walk(dir)
  local ok, names = pcall(fs.list, dir)
  if not ok then out.unreadable = out.unreadable + 1 return end
  table.sort(names)
  for _, name in ipairs(names) do
    if stop then return end
    local full = fs.combine(dir, name)
    if skip_rom and full == "rom" then
      out.rom_skipped = true
    elseif fs.isDir(full) then
      walk(full)
    else
      search(full)
    end
  end
end
if fs.isDir(root) then walk(root) else search(root) end
if bad_pattern then return done({ ok = false, reason = "bad_pattern", message = bad_pattern }) end
out.stopped = stop
return done(out, out.matches)
"""

STAT = r"""
local path = %(path)s
local out = { ok = true, exists = fs.exists(path) }
if not out.exists then return done(out) end
local function try(f, ...)
  if type(f) ~= "function" then return nil end
  local ok, v = pcall(f, ...)
  if ok then return v end
end
out.isDir = fs.isDir(path)
out.isReadOnly = read_only(path)
if out.isDir then
  local names = try(fs.list, path)
  if names then out.entries = #names end
else
  out.size = try(fs.getSize, path)
end
out.drive = try(fs.getDrive, path)
out.capacity = try(fs.getCapacity, path)
out.free = try(fs.getFreeSpace, path)
local attr = try(fs.attributes, path)
if type(attr) == "table" then
  out.created = attr.created
  out.modified = attr.modified or attr.modification
end
return done(out)
"""

DELETE = r"""
local path, recursive = %(path)s, %(recursive)s
if outside(path) then return done({ ok = false, reason = "root" }) end
if not fs.exists(path) then return done({ ok = false, reason = "not_found" }) end
if read_only(path) then return done({ ok = false, reason = "read_only" }) end
local out = { ok = true, path = fs.combine("", path), isDir = fs.isDir(path) }
if out.isDir then
  local names = fs.list(path)
  if #names > 0 and not recursive then return done({ ok = false, reason = "not_empty", entries = #names }) end
  out.files, out.dirs, out.bytes = 0, 0, 0
  local function count(dir)
    for _, name in ipairs(fs.list(dir)) do
      local full = fs.combine(dir, name)
      if fs.isDir(full) then
        out.dirs = out.dirs + 1
        count(full)
      else
        out.files = out.files + 1
        out.bytes = out.bytes + fs.getSize(full)
      end
      tick()
    end
  end
  count(path)
else
  out.size = fs.getSize(path)
end
local ok, err = pcall(fs.delete, path)
if not ok then return done({ ok = false, reason = "failed", message = tostring(err) }) end
if fs.exists(path) then return done({ ok = false, reason = "failed", message = "it still exists after fs.delete" }) end
return done(out)
"""

TRANSFER = r"""
local src, dst, move = %(src)s, %(dst)s, %(move)s
if outside(src) or outside(dst) then return done({ ok = false, reason = "root" }) end
if not fs.exists(src) then return done({ ok = false, reason = "not_found" }) end
if fs.exists(dst) then return done({ ok = false, reason = "exists" }) end
if move and read_only(src) then return done({ ok = false, reason = "read_only", which = "src" }) end
if read_only(dst) then return done({ ok = false, reason = "read_only", which = "dst" }) end
local out = { ok = true, from = fs.combine("", src), to = fs.combine("", dst), isDir = fs.isDir(src) }
if not out.isDir then out.size = fs.getSize(src) end
local ok, err = pcall(move and fs.move or fs.copy, src, dst)
if not ok then return done({ ok = false, reason = "failed", message = tostring(err) }) end
return done(out)
"""

MKDIR = r"""
local path = %(path)s
if outside(path) then return done({ ok = false, reason = "root" }) end
local out = { ok = true, path = fs.combine("", path), existed = fs.exists(path) }
if out.existed then
  if fs.isDir(path) then return done(out) end
  return done({ ok = false, reason = "exists" })
end
if read_only(path) then return done({ ok = false, reason = "read_only" }) end
local ok, err = pcall(fs.makeDir, path)
if not ok then return done({ ok = false, reason = "failed", message = tostring(err) }) end
return done(out)
"""


def _lua_bool(value: bool) -> str:
    return "true" if value else "false"


def find_code(pattern: str, limit: int) -> str:
    """fs.find(pattern), sorted; at most `limit` entries (path, isDir, size) plus the total count."""
    return FS_PRELUDE + FIND % {"pattern": json_literal(pattern), "limit": limit}


def grep_code(pattern: str, root: str, plain: bool, limit: int, include_rom: bool, budget_ms: int) -> str:
    """Search the files under `root` (or the file `root`) line by line; stops after `limit` matches or `budget_ms`."""
    return FS_PRELUDE + GREP % {
        "pattern": json_literal(pattern),
        "root": json_literal(root),
        "plain": _lua_bool(plain),
        "limit": limit,
        "include_rom": _lua_bool(include_rom),
        "budget_ms": budget_ms,
    }


def stat_code(path: str) -> str:
    """exists, isDir, size or entry count, isReadOnly, drive, capacity, free space, created/modified (pcall'd)."""
    return FS_PRELUDE + STAT % {"path": json_literal(path)}


def delete_code(path: str, recursive: bool) -> str:
    """fs.delete after checks: never the root, a missing or read-only path, or (unless recursive) a non-empty dir."""
    return FS_PRELUDE + DELETE % {"path": json_literal(path), "recursive": _lua_bool(recursive)}


def transfer_code(src: str, dst: str, move: bool) -> str:
    """fs.move or fs.copy after checks: `dst` must not exist (CC never overwrites), read-only paths are refused."""
    return FS_PRELUDE + TRANSFER % {"src": json_literal(src), "dst": json_literal(dst), "move": _lua_bool(move)}


def mkdir_code(path: str) -> str:
    """fs.makeDir (parents included); an existing directory is success with existed=true."""
    return FS_PRELUDE + MKDIR % {"path": json_literal(path)}


def parse_text_json(res: dict) -> dict | None:
    """parse_returned_json for the file-system snippets: the JSON object they returned, or None.

    A reply flagged `bytewise` held text that is not valid UTF-8 and was sent one byte per character: decode it
    back, so valid UTF-8 survives and only the invalid bytes become U+FFFD."""
    doc = parse_returned_json(res)
    if not isinstance(doc, dict):
        return None
    return _redecode(doc) if doc.get("bytewise") is True else doc


def _redecode(value):
    if isinstance(value, str):
        try:
            return value.encode("latin-1").decode("utf-8", "replace")
        except UnicodeEncodeError:  # not byte-wise after all
            return value
    if isinstance(value, list):
        return [_redecode(item) for item in value]
    if isinstance(value, dict):
        return {_redecode(key): _redecode(item) for key, item in value.items()}
    return value


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
