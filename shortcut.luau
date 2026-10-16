--!nocheck
-- shortcut.luau —— 控制中心磁贴：一键启停网关
-- 左键：运行中→停止，已停止→启动；右键：打开控制台面板
-- 从「设置 → 控制中心快捷方式」里添加。

local hub = require("./lib/hub.luau")

local watched = false

local function render()
    local snap = hub.state()
    local running = snap and snap.running
    shortcut.setLabel(running and noctalia.tr("shortcut.on") or noctalia.tr("shortcut.off"))
    shortcut.setIcon(running and "player-stop" or "player-play")
    shortcut.setActive(running and true or false)
end

function update()
    if not watched then
        watched = true
        noctalia.state.watch(hub.STATE_KEY, function()
            render()
        end)
    end
    noctalia.setUpdateInterval(math.max(2000, hub.setting("poll_interval_ms")))
    render()
end

function onClick()
    local snap = hub.state()
    if snap and snap.running then
        hub.request("stop")
        shortcut.setLabel(noctalia.tr("shortcut.stopping"))
    else
        hub.request("start")
        shortcut.setLabel(noctalia.tr("shortcut.starting"))
    end
end

function onRightClick()
    noctalia.togglePanel(hub.PANEL)
end

function onIpc(event, payload)
    if tostring(event):lower() == "toggle" then
        onClick()
    end
end
