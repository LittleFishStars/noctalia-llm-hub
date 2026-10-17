--!nocheck
-- lib/hub.luau —— 「大模型控制中心」网关交互层
-- 被 service.luau / bar.luau / panel.luau / shortcut.luau 共享：
--   1) 解析插件设置与路径；2) 以子进程方式驱动 daemon/llmhubd.py；
--   3) 把守护进程的状态汇总成一张快照表，写进 noctalia 共享状态供 UI 读取。
--
-- 设计要点：所有与守护进程的通信都走 CLI（argv 数组形式，无需转义），
-- 因此完全不受宿主 shell.offline_mode 的影响；HTTP 端口只留给外部应用用。

local M = {}

M.PLUGIN = "ylxc/llm-hub"
M.PANEL = "ylxc/llm-hub:console"
M.STATE_KEY = "hub.state"   -- service 发布的快照，UI 用 noctalia.state.watch 订阅
M.CMD_KEY = "hub.cmd"       -- 其它入口写入动作请求，service 监听并执行
M.GLYPH = "router"

local DEFAULT_PORT = 8787
local DEFAULT_POLL_MS = 4000

-- ── 设置 ────────────────────────────────────────────────────────────────────
-- 逐键字面量读取：清单里声明的设置键必须真的被读到（noctalia 静态检查会核对）。

function M.setting(key)
    if key == "autostart" then
        local v = noctalia.getConfig("autostart")
        if v ~= nil then return v end
        return true
    elseif key == "stop_on_exit" then
        local v = noctalia.getConfig("stop_on_exit")
        if v ~= nil then return v end
        return true
    elseif key == "port" then
        local v = noctalia.getConfig("port")
        if type(v) == "number" and v > 0 then return v end
        return DEFAULT_PORT
    elseif key == "host" then
        local v = noctalia.getConfig("host")
        if type(v) == "string" and v ~= "" then return v end
        return "127.0.0.1"
    elseif key == "api_key" then
        local v = noctalia.getConfig("api_key")
        if type(v) == "string" then return v end
        return ""
    elseif key == "config_path" then
        local v = noctalia.getConfig("config_path")
        if type(v) == "string" then return v end
        return ""
    elseif key == "poll_interval_ms" then
        local v = noctalia.getConfig("poll_interval_ms")
        if type(v) == "number" and v >= 1000 then return v end
        return DEFAULT_POLL_MS
    end
    return nil
end

-- ── 路径 ────────────────────────────────────────────────────────────────────

function M.python()
    local p = noctalia.getenv("LLMHUB_PYTHON")
    if type(p) == "string" and p ~= "" then return p end
    return "python3"
end

function M.dir()
    return noctalia.pluginDir()
end

function M.daemon()
    return M.dir() .. "/daemon/llmhubd.py"
end

--- 持久化数据目录：pid/port/log/stats 都放这里，跨插件更新存活。
function M.dataDir()
    local dir = select(1, noctalia.pluginDataDir())
    if type(dir) == "string" and dir ~= "" then
        return dir
    end
    return noctalia.expandPath("~/.local/state/llm-hub")
end

--- 配置文件路径；空串 = 交给守护进程用默认位置（$XDG_CONFIG_HOME/llm-hub/config.json）
function M.configPath()
    local p = M.setting("config_path")
    if type(p) == "string" and p ~= "" then
        return noctalia.expandPath(p)
    end
    return ""
end

function M.portFile() return M.dataDir() .. "/llmhubd.port" end
function M.pidFile() return M.dataDir() .. "/llmhubd.pid" end
function M.logFile() return M.dataDir() .. "/llmhubd.log" end

function M.readPort()
    local txt = noctalia.readFile(M.portFile())
    local n = tonumber((txt or ""):match("^%s*(%-?%d+)"))
    if n then return n end
    return M.setting("port")
end

function M.readPid()
    local txt = noctalia.readFile(M.pidFile())
    local n = tonumber((txt or ""):match("^%s*(%d+)"))
    if n then return n end
    return nil
end

function M.pidAlive()
    local pid = M.readPid()
    if not pid then return false end
    return noctalia.fileExists("/proc/" .. tostring(pid))
end

function M.endpoint()
    return string.format("http://%s:%d", M.setting("host"), M.readPort())
end

-- ── 子进程调用 ──────────────────────────────────────────────────────────────

--- 组装 argv：[python, llmhubd.py, --data-dir D, (--config C), 子命令...]
function M.argv(args)
    local a = { M.python(), M.daemon(), "--data-dir", M.dataDir() }
    local cfg = M.configPath()
    if cfg ~= "" then
        table.insert(a, "--config")
        table.insert(a, cfg)
    end
    for _, v in ipairs(args or {}) do
        table.insert(a, v)
    end
    return a
end

--- 跑一条 CLI 命令。cb(stdout, exitCode)
function M.cli(args, cb, timeoutMs)
    -- runAsync 返回 false 时回调永远不会触发，这里补一次调用，避免调用方的 busy 标志卡死。
    local fired = false
    local started = noctalia.runAsync(M.argv(args), function(res)
        if fired then return end
        fired = true
        if cb then cb(res.stdout or "", res.exitCode or 0) end
    end, timeoutMs or 30000)
    if not started and not fired then
        fired = true
        if cb then cb("", -1) end
        return false
    end
    return started
end

--- 跑 CLI 并解析 JSON 输出。cb(table|nil, exitCode, rawStdout)
--- 从可能混有人类可读行的输出里捞出末尾的 JSON 对象。
--- CLI 有的子命令先打一行「✓ provider:model 123ms」再打 JSON，整段 decode 必然失败；
--- 这里从每个 `{` 起点试到本段最后一个 `}`，取第一个能解出来的。
local function pickJson(text)
    if not text or text == "" then return nil end
    local ok, val = pcall(noctalia.json.decode, text)
    if ok and type(val) == "table" then return val end

    local last = text:find("}", -1, true)
    if not last then return nil end
    local starts = {}
    local pos = 1
    while true do
        local i = text:find("{", pos, true)
        if not i then break end
        table.insert(starts, i)
        pos = i + 1
    end
    for i = #starts, 1, -1 do
        if starts[i] < last then
            local ok2, val2 = pcall(noctalia.json.decode, text:sub(starts[i], last))
            if ok2 and type(val2) == "table" then return val2 end
        end
    end
    return nil
end

--- 跑 CLI 并解析 JSON 输出。cb(table|nil, exitCode, rawStdout)
function M.cliJson(args, cb, timeoutMs)
    return M.cli(args, function(out, code)
        local text = (out or ""):match("^%s*(.-)%s*$") or ""
        if cb then cb(pickJson(text), code, out) end
    end, timeoutMs)
end

-- ── 配置写入（面板里编辑 provider / 规则用）──────────────────────────────

--- 把 Lua 值编码成 JSON 文本（失败返回 nil）
function M.jsonEncode(value)
    local ok, text = pcall(noctalia.json.encode, value)
    if ok and type(text) == "string" then return text end
    return nil
end

--- 改配置里的一个键：M.setPath("providers.mock.base_url", "https://…")
--- jsonValue=true 时 value 按 JSON 解析（数组 / 数字 / 布尔用它）
function M.setPath(key, value, cb, jsonValue)
    local args = { "set", key, tostring(value) }
    if jsonValue then table.insert(args, "--json") end
    M.cliJson(args, function(res, code)
        if cb then cb(res, code) end
    end, 20000)
end

--- 依次执行 ops = {{key, value, json}, …}；全部跑完后 cb(okCount, failCount)
function M.runOps(ops, cb)
    local okN, failN = 0, 0
    local i = 0
    local function step()
        i = i + 1
        local op = ops[i]
        if not op then
            if cb then cb(okN, failN) end
            return
        end
        M.setPath(op[1], op[2], function(_, code)
            if code == 0 then okN = okN + 1 else failN = failN + 1 end
            step()
        end, op[3])
    end
    step()
end

--- 新增 provider：opts = { type, id, label, base_url, key, models = {…} }
--- 用 llmhubd provider-add（能真的往配置数组末尾追加，`set` 做不到）
function M.providerAdd(opts, cb)
    opts = opts or {}
    local args = { "provider-add" }
    local function opt(flag, value)
        if type(value) == "string" and value ~= "" then
            table.insert(args, flag)
            table.insert(args, value)
        end
    end
    opt("--type", opts.type)
    opt("--id", opts.id)
    opt("--label", opts.label)
    opt("--base-url", opts.base_url)
    opt("--key", opts.key)
    for _, m in ipairs(opts.models or {}) do
        opt("--model", m)
    end
    M.cliJson(args, function(res, code)
        if cb then cb(res, code) end
    end, 20000)
end

--- 删除 provider（id 或下标）
function M.providerRemove(id, cb)
    M.cliJson({ "provider-remove", tostring(id) }, function(res, code)
        if cb then cb(res, code) end
    end, 20000)
end

--- 把 "a, b，c" 拆成 {"a","b","c"}（英文/中文逗号、分号、空白都当分隔符）
function M.splitList(text)
    local out = {}
    for item in tostring(text or ""):gmatch("[^,;，；%s]+") do
        table.insert(out, item)
    end
    return out
end
-- ── 生命周期动作 ────────────────────────────────────────────────────────────

--- 确保配置文件存在（首次运行写模板）。cb(ok, message)
function M.ensureConfig(cb)
    local cfg = M.configPath()
    if cfg ~= "" and noctalia.fileExists(cfg) then
        if cb then cb(true, "") end
        return
    end
    M.cli({ "init" }, function(out, code)
        if cb then cb(code == 0, out) end
    end, 20000)
end

--- 把插件设置里的 host/port/api_key 同步进配置文件（只在与现值不同时写）。
--- 这样守护进程、CLI、外部应用三方看到的都是同一份真值。
function M.syncServer(cb)
    M.cliJson({ "config", "--json" }, function(cfg)
        local tasks = {}
        local srv = (cfg and cfg.server) or {}
        local function want(key, value, jsonValue)
            if value == nil then return end
            if srv[key] == value then return end
            table.insert(tasks, { "set", "server." .. key, tostring(value), jsonValue })
        end
        local wantHost = M.setting("host")
        if wantHost ~= "" then want("host", wantHost) end
        want("port", M.setting("port"))
        local key = M.setting("api_key")
        if key ~= "" and srv.api_key ~= "***" and srv.api_key ~= key then
            table.insert(tasks, { "set", "server.api_key", key })
        end
        local i = 0
        local function nextTask()
            i = i + 1
            local t = tasks[i]
            if not t then
                if cb then cb(true) end
                return
            end
            local args = { t[1], t[2], t[3] }
            if t[4] then table.insert(args, "--json") end
            M.cli(args, function(_, code)
                if code ~= 0 then
                    noctalia.log("[llm-hub] set " .. t[2] .. " 失败")
                end
                nextTask()
            end, 20000)
        end
        nextTask()
    end, 20000)
end

--- 启动守护进程（先同步设置，再 start）。cb(ok, stdout)
function M.start(cb)
    M.ensureConfig(function()
        M.syncServer(function()
            M.cli({ "start" }, function(out, code)
                if cb then cb(code == 0, out) end
            end, 60000)
        end)
    end)
end

function M.stop(cb)
    M.cli({ "stop" }, function(out, code)
        if cb then cb(code == 0, out) end
    end, 20000)
end

function M.restart(cb)
    M.ensureConfig(function()
        M.syncServer(function()
            M.cli({ "restart" }, function(out, code)
                if cb then cb(code == 0, out) end
            end, 60000)
        end)
    end)
end

-- ── 快照 ────────────────────────────────────────────────────────────────────

--- 拉取一份状态快照（health + stats + config，可选 logs）。
--- opts = { logs = number|nil }
function M.snapshot(opts, cb)
    opts = opts or {}
    local snap = {
        ts = os.time(),
        running = M.pidAlive(),
        endpoint = M.endpoint(),
        data_dir = M.dataDir(),
        config_path = M.configPath(),
        python = M.python(),
    }
    M.cliJson({ "health" }, function(health, code)
        if health then
            snap.health = health
            snap.running = true
        elseif code ~= 0 and snap.running then
            snap.running = false
        end
        M.cliJson({ "stats", "--json" }, function(stats)
            snap.stats = stats
            M.cliJson({ "config", "--json" }, function(cfg)
                snap.config = cfg
                if not opts.logs then
                    cb(snap)
                    return
                end
                M.cliJson({ "logs", "--json", "-n", tostring(opts.logs) }, function(logs)
                    snap.logs = (logs and logs.logs) or nil
                    cb(snap)
                end, 20000)
            end, 20000)
        end, 20000)
    end, 20000)
end

function M.publish(snap)
    noctalia.state.set(M.STATE_KEY, snap)
end

--- 读最近一次快照（可能是 nil）
function M.state()
    return noctalia.state.get(M.STATE_KEY)
end

--- 请求 service 做一件事：action ∈ start|stop|restart|refresh
function M.request(action)
    noctalia.state.set(M.CMD_KEY, { action = action, at = os.time(), nonce = tostring(os.clock()) })
end

-- ── 格式化 ──────────────────────────────────────────────────────────────────

function M.fmtInt(n)
    n = tonumber(n) or 0
    local s = tostring(math.floor(n))
    local out = s:reverse():gsub("(%d%d%d)", "%1,"):reverse()
    out = out:gsub("^,", "")
    return out
end

function M.fmtNum(n)
    n = tonumber(n)
    if not n then return "-" end
    if n >= 1000000 then return string.format("%.1fM", n / 1000000) end
    if n >= 1000 then return string.format("%.1fk", n / 1000) end
    if n == math.floor(n) then return string.format("%d", n) end
    return string.format("%.1f", n)
end

function M.fmtMs(ms)
    ms = tonumber(ms)
    if not ms then return "-" end
    if ms >= 1000 then return string.format("%.2fs", ms / 1000) end
    return string.format("%dms", math.floor(ms + 0.5))
end

function M.fmtCost(c)
    c = tonumber(c)
    if not c or c <= 0 then return "-" end
    if c < 0.01 then return string.format("$%.4f", c) end
    return string.format("$%.2f", c)
end

function M.fmtUptime(s)
    s = tonumber(s) or 0
    if s >= 86400 then return string.format("%dd%dh", math.floor(s / 86400), math.floor(s % 86400 / 3600)) end
    if s >= 3600 then return string.format("%dh%dm", math.floor(s / 3600), math.floor(s % 3600 / 60)) end
    if s >= 60 then return string.format("%dm%ds", math.floor(s / 60), math.floor(s % 60)) end
    return string.format("%ds", math.floor(s))
end

--- 把 provider 的统计（health/snapshot 的 providers 数组）按 id 建索引
function M.indexProviders(snap)
    local out = {}
    if not snap or not snap.health then return out end
    for _, p in ipairs(snap.health.providers or {}) do
        if type(p) == "table" and p.id then out[p.id] = p end
    end
    return out
end

return M
