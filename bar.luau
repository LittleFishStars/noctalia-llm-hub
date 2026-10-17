--!nocheck
-- bar.luau —— 栏图标：一眼看到网关是否在跑 + 用量概览
-- 左键：打开控制台面板（清单里 [widget.actions] 已绑定）
-- 中键：打开本组件的设置（宿主默认行为）
-- 右键：把本地端点复制到剪贴板

local hub = require("./lib/hub.luau")

local watched = false
local function cfgBool(key, default)
    local v = noctalia.getConfig(key)
    if v == nil then return default end
    return v and true or false
end

local function cfgGlyph()
    local v = noctalia.getConfig("glyph")
    if type(v) == "string" and v ~= "" then return v end
    return hub.GLYPH
end

--- 平均延迟（只算有请求的 provider）
local function avgLatency(snap)
    local n, sum = 0, 0
    for _, p in ipairs((snap and snap.health and snap.health.providers) or {}) do
        local ms = tonumber(p.latency_ms) or 0
        if ms > 0 then n = n + 1; sum = sum + ms end
    end
    if n == 0 then return nil end
    return sum / n
end

local function buildText(snap)
    if not snap or not snap.running then return "" end
    local parts = {}
    -- 栏上不放任何计数：请求次数/Token/成本只放在悬停详情（tooltip）与控制台面板里。
    -- 文字位留给延迟这类轻量信息，且默认关闭（show_latency）。
    if cfgBool("show_latency", false) then
        local ms = avgLatency(snap)
        if ms then table.insert(parts, hub.fmtMs(ms)) end
    end
    return table.concat(parts, " · ")
end

local function buildTooltip(snap)
    local rows = {}
    local function row(k, v) table.insert(rows, { key = k, value = v }) end

    if not snap then
        row(noctalia.tr("bar.state"), noctalia.tr("bar.unknown"))
        row(noctalia.tr("bar.hint"), noctalia.tr("bar.hint_body"))
        return rows
    end

    row(noctalia.tr("bar.state"), snap.running and noctalia.tr("bar.running") or noctalia.tr("bar.stopped"))
    row(noctalia.tr("bar.endpoint"), snap.endpoint or "-")
    if snap.health then
        row(noctalia.tr("bar.uptime"), hub.fmtUptime(snap.health.uptime_s))
        row(noctalia.tr("bar.providers"), string.format("%d / %d",
            snap.health.enabled_providers and #snap.health.enabled_providers or 0,
            snap.health.providers and #snap.health.providers or 0))
    end
    local st = snap.stats or {}
    if st.total_requests then
        row(noctalia.tr("bar.requests"), string.format("%s（%s）", hub.fmtInt(st.total_requests),
            noctalia.tr("stats.errors_n", { count = st.total_errors or 0 })))
        row(noctalia.tr("bar.tokens"), string.format("%s / %s", hub.fmtNum(st.total_tokens_in or 0),
            hub.fmtNum(st.total_tokens_out or 0)))
        row(noctalia.tr("bar.cost"), hub.fmtCost(st.total_cost))
    end
    for _, p in ipairs((snap.health and snap.health.providers) or {}) do
        if p.enabled then
            row(p.id, string.format("%s · %d%s", hub.fmtMs(p.latency_ms), p.requests or 0,
                (p.errors or 0) > 0 and (" · ✗" .. tostring(p.errors)) or ""))
        end
    end
    row(noctalia.tr("bar.hint"), noctalia.tr("bar.hint_body"))
    return rows
end

local function render(snap)
    local running = snap and snap.running
    barWidget.setGlyph(cfgGlyph())
    barWidget.setGlyphColor(running and "primary" or "onSurfaceVariant")
    if cfgBool("show_text", true) then
        barWidget.setText(buildText(snap))
    else
        barWidget.setText("")
    end
    barWidget.setTooltip(buildTooltip(snap))
end

function update()
    if not watched then
        watched = true
        watchState()
    end
    noctalia.setUpdateInterval(math.max(1500, hub.setting("poll_interval_ms")))
    local snap = hub.state()
    render(snap)
    if not snap then
        hub.request("refresh")
    end
end

function watchState()
    noctalia.state.watch(hub.STATE_KEY, function(snap)
        render(snap)
    end)
end

function onClick()
    noctalia.togglePanel(hub.PANEL)
end

function onRightClick()
    local snap = hub.state()
    local ep = (snap and snap.endpoint) or hub.endpoint()
    noctalia.copyToClipboard(ep, "text/plain")
    noctalia.notify("LLM Hub", noctalia.tr("bar.copied", { endpoint = ep }))
end

function onIpc(event, payload)
    if tostring(event):lower() == "refresh" then
        update()
    end
end
