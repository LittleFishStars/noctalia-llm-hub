--!nocheck
-- service.luau —— 网关守护进程的后台服务
-- 负责：按设置自动拉起 llmhubd、周期性把状态写进插件共享状态（供栏图标/面板订阅）、
-- 响应来自其它入口与 `noctalia msg` 的动作命令、插件停用时按设置收尾。
--
-- 共享状态：hub.state（快照） / hub.cmd（动作请求）
-- IPC 事件（`noctalia msg plugin ylxc/llm-hub:hub <event>`）：
--   start / stop / restart / refresh / ask

local hub = require("./lib/hub.luau")

local busy = false
local askSeq = 0

local function poll()
    if busy then return end
    busy = true
    hub.snapshot({}, function(snap)
        snap.autostart = hub.setting("autostart")
        snap.stop_on_exit = hub.setting("stop_on_exit")
        snap.has_key = hub.setting("api_key") ~= ""
        hub.publish(snap)
        busy = false
    end)
end

local function startGateway(quiet)
    hub.start(function(ok, out)
        if ok then
            if not quiet then noctalia.notify("LLM Hub", "网关已启动：" .. hub.endpoint()) end
        else
            noctalia.notifyError("LLM Hub 启动失败", (out or ""):sub(-400))
        end
        poll()
    end)
end

local function stopGateway()
    hub.stop(function()
        poll()
    end)
end

local function handle(action)
    if action == "start" then
        startGateway(true)
    elseif action == "stop" then
        stopGateway()
    elseif action == "restart" then
        hub.restart(function()
            poll()
        end)
    elseif action == "refresh" then
        poll()
    end
end

-- ── 宿主回调 ────────────────────────────────────────────────────────────────

function onEnable()
    noctalia.setUpdateInterval(math.max(1000, hub.setting("poll_interval_ms")))
    noctalia.state.watch(hub.CMD_KEY, function(cmd)
        if type(cmd) == "table" and type(cmd.action) == "string" then
            handle(cmd.action)
        end
    end)
    if hub.setting("autostart") and not hub.pidAlive() then
        startGateway(true)
    else
        poll()
    end
end

function update()
    poll()
end

function onConfigChanged()
    noctalia.setUpdateInterval(math.max(1000, hub.setting("poll_interval_ms")))
    poll()
end

function onIpc(event, payload)
    local e = tostring(event or ""):lower()
    if e == "start" or e == "stop" or e == "restart" or e == "refresh" then
        handle(e)
        return
    end
    if e == "ask" then
        askSeq = askSeq + 1
        hub.cliJson({ "ask", tostring(payload or ""), "--json" }, function(res, code)
            noctalia.state.set("hub.last_ask", {
                seq = askSeq, ok = code == 0, res = res,
                prompt = tostring(payload or ""), out = nil,
            })
        end, 120000)
        return
    end
    if e == "status" then
        poll()
    end
end

function onExit(signal, reason)
    -- reload/热重载时不动网关，避免每次改代码都把服务重启一遍
    if reason == "reload" then return end
    if hub.setting("stop_on_exit") then
        hub.stop()
    end
end
