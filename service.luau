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
local busySince = 0
local askSeq = 0
local lastSig = nil
-- 关键：宿主「开机自动加载插件」时不会调用 onEnable（官方文档里 onEnable 只在插件被
-- 显式启用时触发），所以 state.watch 与 autostart 都不能只写在 onEnable 里 —— 否则每次
-- 重启电脑后都会表现为：面板的「启动/重启」按钮点了没反应、网关也不自动拉起。
local inited = false
local selfHealTried = false
local handle          -- 前向声明：poll 与 ensureInit 都要引用
local startGateway

--- 注册共享状态监听 + 更新间隔（幂等）。由 onEnable 与 update 双路调用兜底。
local function ensureInit()
    if inited then return end
    inited = true
    noctalia.setUpdateInterval(math.max(1000, hub.setting("poll_interval_ms")))
    noctalia.state.watch(hub.CMD_KEY, function(cmd)
        if type(cmd) == "table" and type(cmd.action) == "string" then
            handle(cmd.action)
        end
    end)
end

local function poll()
    local now = noctalia.nowMs()
    if busy then
        -- 上次轮询的回调没回来（进程被杀 / 启动失败）时不能永久卡死：超时强制复位。
        if now - busySince < 30000 then return end
        noctalia.log("llm-hub: 上一次轮询超过 30s 未返回，强制复位后重试")
    end
    busy = true
    busySince = now
    hub.snapshot({}, function(snap)
        snap.autostart = hub.setting("autostart")
        snap.stop_on_exit = hub.setting("stop_on_exit")
        snap.has_key = hub.setting("api_key") ~= ""
        -- 只在状态真变了时落一行日志（排查“面板数据不动”这类问题时很有用）
        local h = snap.health
        local sig = string.format("%s|%s|%s", tostring(snap.running),
            tostring(h and h.version), tostring(h and h.pid))
        if sig ~= lastSig then
            lastSig = sig
            noctalia.log(string.format("llm-hub: 网关%s%s",
                snap.running and "运行中" or "未运行",
                h and h.version and (" · v" .. tostring(h.version)) or ""))
        end
        hub.publish(snap)
        busy = false
        -- 兜底自动拉起（每个会话只试一次，避免和用户手动「停止」打架）：
        -- 开机自动加载时 onEnable 不跑，autostart 只能靠这里补。
        if not snap.running and not selfHealTried and hub.setting("autostart") then
            selfHealTried = true
            noctalia.log("llm-hub: 网关未运行，按 autostart 自动拉起一次")
            startGateway(true)
        end
    end)
end

function startGateway(quiet)   -- 见文件顶部的前向声明
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

function handle(action)        -- 见文件顶部的前向声明
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
    ensureInit()
    if hub.setting("autostart") and not hub.pidAlive() then
        startGateway(true)
    else
        poll()
    end
end

function update()
    ensureInit()   -- 开机自动加载时不会有 onEnable，这里兜底注册监听
    poll()
end

function onConfigChanged()
    ensureInit()
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
