-- bridge.lua - structured remote-exec client for the ComputerCraft MCP relay (computercraft-live-mcp).
--
--   bridge <ws-url> <computer-token>   first run (saved to .bridge.json)
--   bridge                             later runs (reads .bridge.json)
--
-- The bridge HOSTS the computer's shell: a normal shell runs inside it, so players
-- can keep using the computer, while everything shown on the terminal and every
-- printError is recorded (op "log"). That is how errors from programs started by
-- hand become visible. Requests from Claude run in a separate capture terminal:
--   { ok, output, stderr = {...}, returns = {...}, error = {kind, message, traceback} }
-- While a request runs, input events for the hosted shell are queued and replayed
-- afterwards. `exit` in the hosted shell stops the bridge. See docs/REFERENCE.md.

local CONFIG_FILE = ".bridge.json"
local ERROR_FILE = ".bridge-errors.log"
local MAX_MESSAGE = 120000      -- stay under CC's default 128 KiB websocket cap
local DEFAULT_TIMEOUT = 30      -- seconds per request
local CAP_W, CAP_H = 100, 1000  -- capture terminal size
local OUTPUT_LIMIT = 60000      -- max characters of captured output returned
local READ_LIMIT = 60000        -- max bytes returned by one fs read
local HISTORY_MAX = 600         -- scrolled-off terminal lines kept for "log"
local ERRLOG_MAX = 200          -- printError entries kept for "log"
local QUEUE_MAX = 2000          -- events held for the shell while a request runs

-- ---------------------------------------------------------------- config
local args = { ... }
local url, token = args[1], args[2]
if url and not token then error("usage: bridge <ws-url> <token>", 0) end
if url then
  local f = assert(fs.open(CONFIG_FILE, "w"))
  f.write(textutils.serialiseJSON({ url = url, token = token }))
  f.close()
elseif fs.exists(CONFIG_FILE) then
  local f = fs.open(CONFIG_FILE, "r")
  local cfg = textutils.unserialiseJSON(f.readAll())
  f.close()
  if type(cfg) == "table" then url, token = cfg.url, cfg.token end
end
if not url or not token then error("usage: bridge <ws-url> <token>", 0) end

-- ---------------------------------------------------------------- helpers
local realPrintError = printError
local sessionEnv = setmetatable({}, { __index = _ENV })

local function toText(...)
  local t = {}
  for i = 1, select("#", ...) do t[i] = tostring((select(i, ...))) end
  return table.concat(t, " ")
end

local function ser(v)
  local ok, s = pcall(textutils.serialise, v, { compact = true, allow_repetitions = true })
  if ok then return s end
  return tostring(v)
end

local function fail(kind, message, extra)
  local e = { kind = kind, message = message }
  if extra then for k, v in pairs(extra) do e[k] = v end end
  return { ok = false, error = e }
end

local function errorKind(message)
  if tostring(message):find("Terminated$") then return "terminated" end
  return "runtime"
end

-- drop the bridge's own frames so only the user's code shows in a traceback
local function cleanTraceback(tb)
  local keep = {}
  for line in (tb .. "\n"):gmatch("(.-)\n") do
    if line ~= "" and not line:find("bridge.lua", 1, true)
       and not line:find("in function 'xpcall'", 1, true)
       and not line:find("(...tail calls...)", 1, true) then
      keep[#keep + 1] = line
    end
  end
  if #keep <= 1 then return nil end -- header only: nothing useful
  return table.concat(keep, "\n")
end

local function handler(e)
  local msg = type(e) == "string" and e or ser(e)
  local tb
  if debug and debug.traceback then
    local ok, t = pcall(debug.traceback, "", 2)
    if ok and type(t) == "string" then tb = cleanTraceback(t) end
  end
  return { message = msg, traceback = tb }
end

-- Strings are sent as real Unicode when they are valid UTF-8 ("encoding":"utf8").
-- Invalid UTF-8 (CC's border glyphs, binary file data) makes that throw, so fall
-- back to byte-wise escapes ("encoding":"bytes": one \u00XX per raw byte).
local function encode(msg)
  msg.encoding = "utf8"
  local ok, s = pcall(textutils.serialiseJSON, msg, { unicode_strings = true })
  if ok then return true, s end
  msg.encoding = "bytes"
  return pcall(textutils.serialiseJSON, msg)
end

-- cut a string at a character boundary instead of in the middle of a UTF-8 sequence
local function trimUtf8(s)
  return (s:gsub("[\192-\255][\128-\191]*$", ""))
end

local function send(ws, msg)
  if not ws then return end
  for _ = 1, 10 do
    local ok, s = encode(msg)
    if not ok then
      msg = { type = "result", req = msg.req, op = msg.op, ok = false,
              error = { kind = "internal", message = "could not encode result: " .. tostring(s) } }
    elseif #s <= MAX_MESSAGE then
      pcall(ws.send, s)
      return
    else
      for _, key in ipairs({ "output", "data" }) do
        if type(msg[key]) == "string" and #msg[key] > 1000 then
          msg[key] = trimUtf8(msg[key]:sub(1, math.floor(#msg[key] / 2)))
          msg.truncated = true
          if key == "data" then -- a shortened read is not the end of the file
            msg.eof = false
            msg.next_offset = (msg.offset or 0) + #msg.data
          end
        end
      end
    end
  end
end

-- ---------------------------------------------------------------- terminal tee
-- The hosted shell draws on `tee`: every drawing call goes to the real (visible)
-- window and to an invisible shadow window of the same size. Lines that scroll
-- off the top (or get cleared) are copied into `history`, so "log" can show the
-- scrollback plus the current screen.
local startTerm = term.current()
local W0, H0 = startTerm.getSize()
local visible = window.create(startTerm, 1, 1, W0, H0, true)
local shadow = window.create(startTerm, 1, 1, W0, H0, false)

local history = {}

local function isRed(text, fg)
  for i = 1, #text do
    if text:sub(i, i) ~= " " and fg:sub(i, i) == "e" then return true end
  end
  return false
end

local function pushHistory(text, fg)
  local trimmed = text:gsub("%s+$", "")
  if trimmed == "" then return end
  history[#history + 1] = { text = trimmed, red = isRed(text, fg) }
  if #history > HISTORY_MAX then table.remove(history, 1) end
end

local function recordTop(n)
  local _, h = shadow.getSize()
  for y = 1, math.min(n, h) do pushHistory(shadow.getLine(y)) end
end

local tee = setmetatable({}, { __index = function(_, k)
  local v = visible[k]
  if type(v) == "function" then return function(...) return v(...) end end
  return v
end })

for _, name in ipairs({ "write", "blit", "clearLine", "setCursorPos", "setCursorBlink",
                        "setTextColor", "setTextColour", "setBackgroundColor",
                        "setBackgroundColour", "setPaletteColor", "setPaletteColour" }) do
  tee[name] = function(...)
    shadow[name](...)
    return visible[name](...)
  end
end
function tee.scroll(n)
  if type(n) == "number" and n > 0 then recordTop(n) end
  shadow.scroll(n)
  return visible.scroll(n)
end
function tee.clear()
  local _, h = shadow.getSize()
  recordTop(h)
  shadow.clear()
  return visible.clear()
end

-- ---------------------------------------------------------------- printError log
local errSeq, errlog = 0, {}

local function appendErrorFile(line)
  pcall(function()
    if fs.exists(ERROR_FILE) and fs.getSize(ERROR_FILE) > 30000 then
      local f = fs.open(ERROR_FILE, "rb")
      local all = f.readAll()
      f.close()
      f = fs.open(ERROR_FILE, "wb")
      f.write(all:sub(-15000))
      f.close()
    end
    local f = fs.open(ERROR_FILE, "ab")
    f.write(line .. "\n")
    f.close()
  end)
end

local function recordError(text)
  errSeq = errSeq + 1
  local when = ("day %d %s"):format(os.day(), textutils.formatTime(os.time(), true))
  errlog[#errlog + 1] = { id = errSeq, when = when, text = text }
  if #errlog > ERRLOG_MAX then table.remove(errlog, 1) end
  appendErrorFile("[" .. when .. "] " .. text)
end

local function teePrintError(...)
  recordError(toText(...))
  return realPrintError(...)
end
_G.printError = teePrintError

-- bridge status lines go through the tee so they show on screen; "log" filters them out
local function say(msg, isError)
  local previous = term.redirect(tee)
  ;(isError and realPrintError or print)("[bridge] " .. msg)
  term.redirect(previous)
end

-- optional connection trace: create a file named .bridge-debug to enable
local function dbg(msg)
  if not fs.exists(".bridge-debug") then return end
  local f = fs.open(".bridge-debug.log", "ab")
  if f then f.write(("%.2f %s\n"):format(os.clock(), msg)); f.close() end
end

-- ---------------------------------------------------------------- operations
local ops = {}

function ops.ping()
  return { ok = true, id = os.getComputerID(), label = os.getComputerLabel(),
           clock = os.clock(), day = os.day(), host = _HOST, free = fs.getFreeSpace("/") }
end

function ops.log(req)
  local want = math.min(tonumber(req.lines) or 50, 300)
  local since = tonumber(req.since_error) or 0
  local lines, red = {}, {}
  if not req.errors_only then
    local all = {}
    for _, h in ipairs(history) do
      if not h.text:find("[bridge]", 1, true) then all[#all + 1] = h end
    end
    local _, hh = shadow.getSize()
    for y = 1, hh do
      local text, fg = shadow.getLine(y)
      local trimmed = text:gsub("%s+$", "")
      if trimmed ~= "" and not trimmed:find("[bridge]", 1, true) then
        all[#all + 1] = { text = trimmed, red = isRed(text, fg) }
      end
    end
    for i = math.max(1, #all - want + 1), #all do
      lines[#lines + 1] = all[i].text
      if all[i].red then red[#red + 1] = #lines end
    end
  end
  local errors = {}
  for _, e in ipairs(errlog) do
    if e.id > since then errors[#errors + 1] = { id = e.id, when = e.when, text = e.text } end
  end
  return { ok = true, lines = lines, red = red, errors = errors, last_error_id = errSeq }
end

function ops.exec(req)
  if type(req.code) ~= "string" then return fail("bad_request", "exec needs 'code'") end
  if req.reset then sessionEnv = setmetatable({}, { __index = _ENV }) end
  -- REPL trick: try it as an expression first so `os.getComputerID()` returns a value
  local fn = load("return " .. req.code, "=exec", "t", sessionEnv)
  if not fn then
    local err
    fn, err = load(req.code, "=exec", "t", sessionEnv)
    if not fn then return fail("syntax", tostring(err)) end
  end
  local res = table.pack(xpcall(fn, handler))
  if not res[1] then
    local e = res[2]
    -- CC's "Too long without yielding" abort bypasses our handler and hands back a bare value
    if type(e) ~= "table" or e.message == nil then e = { message = type(e) == "string" and e or ser(e) } end
    return fail(errorKind(e.message), e.message, { traceback = e.traceback })
  end
  local returns = {}
  for i = 2, res.n do returns[#returns + 1] = ser(res[i]) end
  return { ok = true, returns = returns }
end

function ops.shell(req)
  if type(req.command) ~= "string" then return fail("bad_request", "shell needs 'command'") end
  local ok = shell.run(req.command)
  -- when ok is false the stderr lines are folded into error by finish()
  return { ok = ok and true or false }
end

function ops.read(req)
  local path = req.path
  if type(path) ~= "string" then return fail("bad_request", "read needs 'path'") end
  if not fs.exists(path) then return fail("runtime", "No such file: " .. path) end
  if fs.isDir(path) then return fail("runtime", "Is a directory: " .. path) end
  local f, err = fs.open(path, "rb")
  if not f then return fail("runtime", tostring(err)) end
  local offset = tonumber(req.offset) or 0
  local limit = math.min(tonumber(req.limit) or READ_LIMIT, READ_LIMIT)
  if offset > 0 then f.seek("set", offset) end
  local data = f.read(limit) or ""
  f.close()
  local size = fs.getSize(path)
  return { ok = true, data = data, size = size, offset = offset, next_offset = offset + #data,
           eof = offset + #data >= size }
end

function ops.write(req)
  local path = req.path
  if type(path) ~= "string" or type(req.data) ~= "string" then
    return fail("bad_request", "write needs 'path' and 'data'")
  end
  if fs.isReadOnly(path) then return fail("runtime", "Read-only path: " .. path) end
  local dir = fs.getDir(path)
  if dir ~= "" and not fs.exists(dir) then fs.makeDir(dir) end
  local f, err = fs.open(path, req.append and "ab" or "wb")
  if not f then return fail("runtime", tostring(err)) end
  f.write(req.data)
  f.close()
  return { ok = true, size = fs.getSize(path) }
end

function ops.list(req)
  local path = req.path or ""
  if not fs.exists(path) then return fail("runtime", "No such directory: " .. path) end
  if not fs.isDir(path) then return fail("runtime", "Not a directory: " .. path) end
  local entries = {}
  for _, name in ipairs(fs.list(path)) do
    local full = fs.combine(path, name)
    entries[#entries + 1] = { name = name, isDir = fs.isDir(full), size = fs.isDir(full) and 0 or fs.getSize(full) }
  end
  return { ok = true, entries = entries }
end

function ops.peripherals(req)
  local out = {}
  for _, name in ipairs(peripheral.getNames()) do
    local entry = { name = name, types = { peripheral.getType(name) } }
    if req.methods then entry.methods = peripheral.getMethods(name) end
    out[#out + 1] = entry
  end
  return { ok = true, peripherals = out }
end

local function dispatch(req)
  local f = ops[req.op]
  if not f then return fail("bad_request", "unknown op: " .. tostring(req.op)) end
  return f(req)
end

-- ---------------------------------------------------------------- jobs
-- One request runs at a time, in its own coroutine, with the terminal
-- redirected into an off-screen window and printError wrapped.
local job = nil
local finish, step

local function readCapture(win)
  local _, cy = win.getCursorPos()
  local lines = {}
  for y = 1, math.min(cy, CAP_H) do
    lines[y] = (win.getLine(y):gsub("%s+$", ""))
  end
  while #lines > 0 and lines[#lines] == "" do lines[#lines] = nil end
  local text = table.concat(lines, "\n")
  local truncated = cy >= CAP_H
  if #text > OUTPUT_LIMIT then
    -- keep the tail (errors are usually last), starting on a character boundary
    text = (text:sub(-OUTPUT_LIMIT):gsub("^[\128-\191]+", ""))
    truncated = true
  end
  return text, truncated
end

function finish(result)
  local j = job
  job = nil
  os.cancelTimer(j.timer)
  _G.printError = teePrintError
  term.redirect(j.previous)
  local output, truncated = readCapture(j.win)
  if result.ok == false and not result.error then
    local msg = #j.stderr > 0 and table.concat(j.stderr, "\n") or "command returned false"
    result.error = { kind = "exit", message = msg }
  end
  result.type, result.req, result.op = "result", j.req.req, j.req.op
  result.output, result.stderr = output, j.stderr
  result.truncated = truncated or nil
  result.duration = os.clock() - j.started
  send(j.ws, result)
  say(("%s %s %.1fs"):format(j.req.op, result.ok and "ok" or "ERR", result.duration))
end

function step(ev)
  local ok, a
  if ev then ok, a = coroutine.resume(job.co, table.unpack(ev, 1, ev.n))
  else ok, a = coroutine.resume(job.co) end
  if not ok then
    local msg = tostring(a)
    finish(fail(errorKind(msg) == "terminated" and "terminated" or "internal", msg))
  elseif coroutine.status(job.co) == "dead" then
    finish(type(a) == "table" and a or fail("internal", "operation returned no result"))
  else
    job.filter = a
  end
end

local function startJob(ws, req)
  local win = window.create(startTerm, 1, 1, CAP_W, CAP_H, false)
  job = { req = req, ws = ws, win = win, started = os.clock(), stderr = {} }
  job.previous = term.redirect(win)
  local j = job
  _G.printError = function(...)
    j.stderr[#j.stderr + 1] = toText(...)
    return realPrintError(...)
  end
  job.timer = os.startTimer(tonumber(req.timeout) or DEFAULT_TIMEOUT)
  job.co = coroutine.create(function() return dispatch(req) end)
  step(nil)
end

local function onMessage(ws, raw)
  local req = textutils.unserialiseJSON(raw)
  if type(req) ~= "table" or req.type ~= "req" then return end
  local function reply(r) r.type, r.req, r.op = "result", req.req, req.op; send(ws, r) end
  if req.op == "ping" then return reply(ops.ping()) end
  if req.op == "log" then return reply(ops.log(req)) end -- reads state only, no capture needed
  if req.op == "cancel" then
    if job then finish(fail("cancelled", "cancelled by request")) end
    return reply({ ok = true, cancelled = true })
  end
  if job then return reply(fail("busy", "another request is still running")) end
  startJob(ws, req)
end

-- ---------------------------------------------------------------- hosted shell
local host = nil          -- { co, filter }
local hostDone = false    -- the shell exited normally: stop the bridge
local hostRestartTimer = nil
local hostQueue = {}      -- events held back while a request runs

local function resumeHost(ev)
  if not host then return end
  if ev and host.filter and host.filter ~= ev[1] and ev[1] ~= "terminate" then return end
  local ok, a
  if ev then ok, a = coroutine.resume(host.co, table.unpack(ev, 1, ev.n))
  else ok, a = coroutine.resume(host.co) end
  if not ok then
    host = nil
    recordError("bridge: hosted shell crashed: " .. tostring(a))
    say("hosted shell crashed: " .. tostring(a) .. " - restarting", true)
    hostRestartTimer = os.startTimer(1)
  elseif coroutine.status(host.co) == "dead" then
    host = nil
    hostDone = true
  else
    host.filter = a
  end
end

local function startHost()
  host = { co = coroutine.create(function() return shell.run("shell") end) }
  resumeHost(nil)
end

local function queueHost(ev)
  hostQueue[#hostQueue + 1] = ev
  if #hostQueue > QUEUE_MAX then table.remove(hostQueue, 1) end
end

local function flushQueue()
  local q = hostQueue
  hostQueue = {}
  for _, ev in ipairs(q) do resumeHost(ev) end
end

-- ---------------------------------------------------------------- connection
-- Non-blocking: the shell keeps running while we (re)connect.
local conn = { ws = nil, state = "down", delay = 2 }

local function scheduleRetry(seconds)
  conn.state = "down"
  if conn.retryTimer then os.cancelTimer(conn.retryTimer) end
  conn.retryTimer = os.startTimer(seconds or conn.delay)
  conn.delay = math.min(conn.delay * 2, 30)
end

local function closeConn()
  local ws = conn.ws
  conn.ws, conn.state = nil, "down"
  if ws then pcall(ws.close) end
end

local function tryConnect()
  dbg("tryConnect (state was " .. conn.state .. ")")
  -- Events carry only the URL, so we cannot tell two connections apart: never have
  -- two at once. A retry timer that fires while a connection exists is stale.
  if conn.state ~= "down" then return end
  conn.state = "connecting"
  conn.retryTimer = nil
  local ok, err = pcall(http.websocketAsync, url)
  if not ok then
    say("cannot open websocket: " .. tostring(err), true)
    scheduleRetry(30)
  end
end

local function onWsSuccess(ws)
  dbg("ws success (state was " .. conn.state .. ")")
  conn.ws, conn.state = ws, "hello"
  ws.send(textutils.serialiseJSON({ type = "hello", token = token, id = os.getComputerID(),
                                    label = os.getComputerLabel(), host = _HOST }))
  conn.helloTimer = os.startTimer(10)
end

local function onWsFailure(err)
  say("connect failed: " .. tostring(err) .. " (retrying)", true)
  scheduleRetry()
end

local function onWsMessage(raw)
  if conn.state == "hello" then
    local ok, msg = pcall(textutils.unserialiseJSON, raw or "")
    if ok and type(msg) == "table" and msg.type == "welcome" then
      dbg("welcome -> up")
      conn.state, conn.delay = "up", 2
      if conn.retryTimer then os.cancelTimer(conn.retryTimer); conn.retryTimer = nil end
      say("connected")
    else
      say("handshake rejected (wrong token?)", true)
      closeConn()
      scheduleRetry(30)
    end
  elseif conn.state == "up" then
    onMessage(conn.ws, raw)
  end
end

local function onWsClosed()
  dbg("ws closed (state was " .. conn.state .. ")")
  local was = conn.state
  -- "down": we closed it ourselves. "connecting": there is no connection yet, so this
  -- event is stale (e.g. left over from before a reboot) and must not trigger a retry.
  if was == "down" or was == "connecting" then return end
  conn.ws = nil
  if job then finish(fail("disconnected", "connection lost during request")) end
  if was == "up" then say("connection lost; reconnecting", true) end
  scheduleRetry(2)
end

-- ---------------------------------------------------------------- main loop
-- Two bridges on one computer would keep replacing each other's connection.
-- _G is shared by every program on the computer and is wiped on reboot.
if _G.__claude_bridge_running then error("the bridge is already running on this computer", 0) end
_G.__claude_bridge_running = true

local function main()
  dbg("bridge main start, clock " .. os.clock())
  term.redirect(tee)
  tee.clear()
  tee.setCursorPos(1, 1)
  say("started (#" .. os.getComputerID() .. "); `exit` stops it")
  startHost()
  tryConnect()

  while not hostDone do
    local ev = table.pack(os.pullEventRaw())
    local name, a = ev[1], ev[2]
    local mine = false
    if name == "websocket_success" and a == url then
      mine = true
      onWsSuccess(ev[3])
    elseif name == "websocket_failure" and a == url then
      mine = true
      onWsFailure(ev[3])
    elseif name == "websocket_message" and a == url then
      mine = true
      onWsMessage(ev[3])
    elseif name == "websocket_closed" and a == url then
      mine = true
      onWsClosed()
    elseif name == "timer" then
      if a == conn.retryTimer then
        mine = true
        tryConnect()
      elseif a == conn.helloTimer then
        mine = true
        conn.helloTimer = nil
        if conn.state == "hello" then closeConn(); scheduleRetry(5) end
      elseif a == hostRestartTimer then
        mine = true
        hostRestartTimer = nil
        startHost()
      elseif job and a == job.timer then
        mine = true
        finish(fail("timeout", "request exceeded its timeout and was abandoned"))
      end
    elseif name == "term_resize" then
      local w, h = startTerm.getSize()
      visible.reposition(1, 1, w, h)
      shadow.reposition(1, 1, w, h)
    end
    if not mine then
      if job then
        if job.filter == nil or job.filter == name or name == "terminate" then step(ev) end
        queueHost(ev) -- the shell sees it once the request is done
      else
        resumeHost(ev)
      end
    end
    if not job and #hostQueue > 0 then flushQueue() end
  end
end -- main

local ok, err = pcall(main)

-- the hosted shell exited (or the bridge crashed): tidy up and hand the terminal back
closeConn()
_G.printError = realPrintError
_G.__claude_bridge_running = nil
term.redirect(startTerm)
startTerm.clear()
startTerm.setCursorPos(1, 1)
if not ok then error(err, 0) end
print("bridge stopped.")
