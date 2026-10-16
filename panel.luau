--!nocheck
-- panel.luau —— 「大模型控制中心」控制台
-- 五个页签：概览 / Provider / 路由规则 / 问答 / 日志
-- 数据来源：service 发布的共享状态快照（hub.state）；动作则直接调 llmhubd CLI。
-- 打开方式：左键点栏图标，或 `noctalia msg panel-toggle ylxc/llm-hub:console`

local hub = require("./lib/hub.luau")

local view = {
    snap = nil,
    tab = "overview",
    logs = nil,
    logsBusy = false,
    note = nil,          -- 最近一次动作的结果提示
    test = nil,          -- { id, ok, text }
    askText = "",
    askModel = "default",
    askModelIdx = 0,
    answer = nil,        -- { ok, text, provider, model, rule, ms }
    asking = false,
    routeModel = "*",
    route = nil,
    watched = false,
    dirty = false,
}

local function L(key, subst) return noctalia.tr(key, subst) end

local function refresh()
    view.dirty = true
    hub.request("refresh")
    if not view.snap and not view.logsBusy then
        view.logsBusy = true
        hub.snapshot({ logs = 20 }, function(snap)
            view.snap = snap
            view.logs = snap.logs
            view.logsBusy = false
            render()
        end)
    end
end

local function fetchLogs()
    if view.logsBusy then return end
    view.logsBusy = true
    hub.cliJson({ "logs", "--json", "-n", "25" }, function(res)
        view.logs = (res and res.logs) or {}
        view.logsBusy = false
        render()
    end)
end

local function run(label, args, timeoutMs)
    view.note = L("panel.busy", { label = label })
    render()
    hub.cliJson(args, function(res, code)
        if code == 0 then
            view.note = L("panel.done", { label = label })
        else
            local detail = type(res) == "table" and (res.error or res.detail) or nil
            view.note = L("panel.failed", { label = label, detail = detail or code })
        end
        refresh()
        render()
    end, timeoutMs or 60000)
end

local function providerList()
    local out = {}
    local cfg = view.snap and view.snap.config
    local byId = hub.indexProviders(view.snap)
    if cfg and cfg.providers then
        for i, p in ipairs(cfg.providers) do
            local h = byId[p.id] or {}
            table.insert(out, {
                index = i - 1, id = p.id, label = p.label or p.id, type = p.type,
                enabled = p.enabled and true or false,
                models = p.models or {},
                models_n = #(p.models or {}),
                priority = p.priority,
                has_key = p.has_key, missing_env = p.missing_env,
                requests = h.requests or p.requests or 0,
                errors = h.errors or p.errors or 0,
                latency_ms = h.latency_ms or p.latency_ms,
                last_error = h.last_error or p.last_error,
            })
        end
    elseif view.snap and view.snap.health then
        for _, p in ipairs(view.snap.health.providers or {}) do
            table.insert(out, {
                index = nil, id = p.id, label = p.id, type = p.type,
                enabled = p.enabled and true or false,
                models = {}, models_n = 0,
                requests = p.requests or 0, errors = p.errors or 0,
                latency_ms = p.latency_ms,
            })
        end
    end
    return out
end

--- provider 启停：网关在跑就走 /admin 接口（立即生效并写回配置），
--- 否则直接改配置文件里的对应下标（下次启动生效）。
local function setEnabled(p, want)
    if p.index == nil then return end
    if view.snap and view.snap.running then
        hub.cliJson({ want and "enable" or "disable", p.id }, function() refresh(); render() end)
        return
    end
    hub.cliJson({ "set", "providers." .. tostring(p.index) .. ".enabled",
                  want and "true" or "false" }, function() refresh(); render() end)
end

local function modelOptions()
    local opts = { "default" }
    local seen = { default = true }
    local cfg = view.snap and view.snap.config
    for name in pairs((cfg and cfg.aliases) or {}) do
        if not seen[name] then seen[name] = true; table.insert(opts, name) end
    end
    for _, p in ipairs((cfg and cfg.providers) or {}) do
        for _, m in ipairs(p.models or {}) do
            if type(m) == "string" and m ~= "*" and not seen[m] and #opts < 40 then
                seen[m] = true
                table.insert(opts, m)
            end
        end
    end
    return opts
end

-- ── 渲染小工具 ──────────────────────────────────────────────────────────────

local function tile(label, value, glyph, color)
    return ui.column({ fill = "surface_variant", radius = 8, padding = 8, gap = 2, flexGrow = 1,
                       align = "stretch" }, {
        ui.row({ gap = 6, align = "center" }, {
            ui.glyph({ name = glyph, size = 13, color = color or "primary" }),
            ui.label({ text = label, fontSize = 10, color = "on_surface_variant" }),
        }),
        ui.label({ text = value, fontSize = 15, fontWeight = "semibold", color = "on_surface" }),
    })
end

local function sectionTitle(text, glyph)
    return ui.row({ gap = 6, align = "center" }, {
        ui.glyph({ name = glyph or "chevron-down", size = 13, color = "primary" }),
        ui.label({ text = text, fontSize = 12, fontWeight = "semibold", color = "on_surface" }),
    })
end

local function kv(k, v, color)
    return ui.row({ gap = 8, align = "center" }, {
        ui.label({ text = k, fontSize = 11, color = "on_surface_variant", width = 150,
                   maxLines = 1 }),
        ui.label({ text = v == nil and "-" or tostring(v), fontSize = 11, flexGrow = 1,
                   color = color or "on_surface", maxLines = 1 }),
    })
end

-- ── 页签：概览 ──────────────────────────────────────────────────────────────

local function tabOverview()
    local snap = view.snap
    local st = (snap and snap.stats) or {}
    local children = {}

    table.insert(children, ui.row({ gap = 8 }, {
        tile(L("panel.tile.requests"), hub.fmtInt(st.total_requests or 0), "activity"),
        tile(L("panel.tile.errors"), hub.fmtInt(st.total_errors or 0), "alert-triangle", "error"),
        tile(L("panel.tile.tokens"), hub.fmtNum((st.total_tokens_in or 0) + (st.total_tokens_out or 0)),
             "database"),
        tile(L("panel.tile.cost"), hub.fmtCost(st.total_cost), "bolt"),
    }))

    if snap and snap.health then
        table.insert(children, ui.column({ fill = "surface_variant", radius = 8, padding = 10, gap = 4 }, {
            sectionTitle(L("panel.overview.runtime"), "server"),
            kv(L("panel.overview.endpoint"), snap.endpoint),
            kv(L("panel.overview.uptime"), hub.fmtUptime(snap.health.uptime_s)),
            kv(L("panel.overview.providers"), string.format("%d / %d",
                snap.health.enabled_providers and #snap.health.enabled_providers or 0,
                snap.health.providers and #snap.health.providers or 0)),
            kv(L("panel.overview.config"), (snap.config_path ~= "" and snap.config_path)
                or L("panel.overview.config_default")),
            kv(L("panel.overview.datadir"), snap.data_dir),
            kv(L("panel.overview.version"), snap.health.version),
            kv(L("panel.overview.auth"), snap.has_key and L("panel.overview.auth_on")
                or L("panel.overview.auth_off")),
        }))
    else
        table.insert(children, ui.column({ fill = "surface_variant", radius = 8, padding = 12 }, {
            ui.label({ text = L("panel.not_running"), fontSize = 12, color = "on_surface_variant" }),
        }))
    end

    local rules = (snap and snap.config and snap.config.rules) or {}
    if #rules > 0 then
        local rows = { sectionTitle(L("panel.overview.rules"), "arrows-split-2") }
        for i, r in ipairs(rules) do
            if i > 6 then break end
            table.insert(rows, kv(r.id or ("#" .. tostring(i)),
                string.format("%s · %s", (r.route and r.route.tactic) or r.tactic or "priority",
                    #((r.route and r.route.providers) or r.providers or {}) .. L("panel.unit.providers"))))
        end
        table.insert(children, ui.column({ fill = "surface_variant", radius = 8, padding = 10, gap = 4 }, rows))
    end

    return ui.column({ gap = 10, padding = 12 }, children)
end

-- ── 页签：Provider ──────────────────────────────────────────────────────────

local function tabProviders()
    local list = providerList()
    local children = { sectionTitle(L("panel.providers.title", { count = #list }), "server-2") }
    if #list == 0 then
        table.insert(children, ui.label({ text = L("panel.providers.empty"), fontSize = 11,
                                          color = "on_surface_variant" }))
    end
    for _, p in ipairs(list) do
        local status = p.enabled and L("panel.providers.on") or L("panel.providers.off")
        local color = p.enabled and "primary" or "on_surface_variant"
        local detail = string.format("%s · %s · %d%s", p.type or "?", status, p.models_n,
                                     L("panel.unit.models"))
        if p.missing_env and #p.missing_env > 0 then
            detail = detail .. " · " .. L("panel.providers.missing_env") .. " " .. table.concat(p.missing_env, ",")
        end
        if not p.has_key and p.type ~= "mock" and p.type ~= "ollama" then
            detail = detail .. " · " .. L("panel.providers.no_key")
        end
        local stats = string.format("%s · %s", hub.fmtInt(p.requests or 0), hub.fmtMs(p.latency_ms))
        if (p.errors or 0) > 0 then
            stats = stats .. " · ✗ " .. tostring(p.errors)
        end
        table.insert(children, ui.column({ fill = "surface_variant", radius = 8, padding = 8, gap = 4 }, {
            ui.row({ gap = 8, align = "center" }, {
                ui.glyph({ name = p.enabled and "plug-connected" or "plug", size = 14, color = color }),
                ui.column({ gap = 1, flexGrow = 1 }, {
                    ui.label({ text = p.label, fontSize = 12, fontWeight = "medium", color = "on_surface",
                               maxLines = 1 }),
                    ui.label({ text = detail, fontSize = 10, color = "on_surface_variant", maxLines = 1 }),
                }),
                ui.label({ text = stats, fontSize = 10, color = "on_surface_variant", maxLines = 1 }),
                ui.button({ text = p.enabled and L("panel.providers.disable") or L("panel.providers.enable"),
                            variant = p.enabled and "outline" or "primary", controlSize = "sm",
                            onClick = function() setEnabled(p, not p.enabled) end }),
                ui.button({ glyph = "wand", variant = "ghost", controlSize = "sm",
                            tooltip = L("panel.providers.test"),
                            onClick = function()
                                view.test = { id = p.id, ok = nil }
                                render()
                                hub.cliJson({ "test", p.id, "--json" }, function(res, code)
                                    view.test = { id = p.id, ok = code == 0,
                                                  text = (res and res.text) or L("panel.providers.test_fail") }
                                    render()
                                end, 60000)
                            end }),
            }),
            ui.label({ text = p.last_error or "", fontSize = 10, color = "error", maxLines = 1,
                       visible = (p.last_error ~= nil and p.last_error ~= "") }),
        }))
    end
    if view.test then
        table.insert(children, ui.column({ fill = "surface_variant", radius = 8, padding = 8, gap = 2 }, {
            ui.label({ text = L("panel.providers.test_result", { id = view.test.id }), fontSize = 11,
                       fontWeight = "medium", color = view.test.ok and "primary" or "error" }),
            ui.label({ text = (view.test.text or ""):sub(1, 300), fontSize = 10, maxLines = 3,
                       color = "on_surface_variant" }),
        }))
    end
    return ui.column({ gap = 8, padding = 12 }, children)
end

-- ── 页签：路由规则 ──────────────────────────────────────────────────────────

local function whenSummary(when)
    if type(when) ~= "table" then return L("panel.rules.always") end
    local parts = {}
    for k, v in pairs(when) do
        if type(v) == "table" then
            table.insert(parts, string.format("%s=%s", k, table.concat(v, ",")))
        else
            table.insert(parts, string.format("%s=%s", k, tostring(v)))
        end
    end
    if #parts == 0 then return L("panel.rules.always") end
    return table.concat(parts, "  ")
end

local function tabRules()
    local cfg = (view.snap and view.snap.config) or {}
    local children = { sectionTitle(L("panel.rules.title"), "arrows-split-2") }

    local function ruleCard(title, tactic, providers, when, note)
        local provs = {}
        for _, s in ipairs(providers or {}) do
            table.insert(provs, tostring(s))
        end
        return ui.column({ fill = "surface_variant", radius = 8, padding = 8, gap = 3 }, {
            ui.row({ gap = 8, align = "center" }, {
                ui.glyph({ name = "bolt", size = 13, color = "primary" }),
                ui.label({ text = title, fontSize = 12, fontWeight = "medium", flexGrow = 1,
                           color = "on_surface", maxLines = 1 }),
                ui.label({ text = tactic or "priority", fontSize = 10, color = "primary" }),
            }),
            ui.label({ text = L("panel.rules.when") .. " " .. when, fontSize = 10,
                       color = "on_surface_variant", maxLines = 2, height = 30 }),
            ui.label({ text = "→ " .. (#provs > 0 and table.concat(provs, " → ") or "*"),
                       fontSize = 10, color = "on_surface", maxLines = 2 }),
            ui.label({ text = note or "", fontSize = 10, color = "on_surface_variant",
                       visible = note ~= nil, maxLines = 1 }),
        })
    end

    for _, r in ipairs(cfg.rules or {}) do
        local route = r.route or r
        table.insert(children, ruleCard(r.id or "?", route.tactic, route.providers,
                                        whenSummary(r.when), r.description))
    end
    local d = cfg.default_route or {}
    table.insert(children, ruleCard(L("panel.rules.default"), d.tactic, d.providers, L("panel.rules.always"),
                                    d.fallback and L("panel.rules.fallback_on") or nil))

    local aliases = cfg.aliases or {}
    local akeys = {}
    for k in pairs(aliases) do table.insert(akeys, k) end
    table.sort(akeys)
    if #akeys > 0 then
        local rows = { sectionTitle(L("panel.rules.aliases"), "building-broadcast-tower") }
        for _, k in ipairs(akeys) do
            table.insert(rows, kv(k, aliases[k]))
        end
        table.insert(children, ui.column({ fill = "surface_variant", radius = 8, padding = 10, gap = 4 }, rows))
    end

    local scen = cfg.scenarios or {}
    local skeys = {}
    for k in pairs(scen) do table.insert(skeys, k) end
    table.sort(skeys)
    if #skeys > 0 then
        local rows = { sectionTitle(L("panel.rules.scenarios"), "topology-star-3") }
        for _, k in ipairs(skeys) do
            local s = scen[k] or {}
            table.insert(rows, kv(k, string.format("%s · %d %s", s.tactic or "priority",
                #(s.providers or {}), L("panel.unit.providers"))))
        end
        table.insert(children, ui.column({ fill = "surface_variant", radius = 8, padding = 10, gap = 4 }, rows))
    end
    return ui.column({ gap = 8, padding = 12 }, children)
end

-- ── 页签：问答 ──────────────────────────────────────────────────────────────

local function tabAsk()
    local children = { sectionTitle(L("panel.ask.title"), "messages") }

    table.insert(children, ui.row({ gap = 8, align = "stretch" }, {
        ui.input({ key = "ask.input", placeholder = L("panel.ask.placeholder"), flexGrow = 1,
                   controlSize = "md", submitOnEnter = true, value = view.askText,
                   onChange = function(v) view.askText = v end,
                   onSubmit = function(v) view.askText = v; askNow() end }),
        ui.select({ key = "ask.model", options = modelOptions(), selectedIndex = view.askModelIdx,
                    controlSize = "md", width = 170,
                    onChange = function(idx, text)
                        view.askModelIdx = tonumber(idx) or 0
                        view.askModel = text or "default"
                    end }),
        ui.button({ text = L("panel.ask.send"), glyph = "send", variant = "primary", controlSize = "md",
                    enabled = not view.asking, onClick = function() askNow() end }),
    }))

    table.insert(children, ui.column({ fill = "surface_variant", radius = 8, padding = 10, gap = 4 }, {
        ui.row({ gap = 8, align = "center" }, {
            ui.glyph({ name = "wand", size = 13, color = "primary" }),
            ui.label({ text = L("panel.route.title"), fontSize = 11, color = "on_surface" }),
            ui.input({ key = "route.input", value = view.routeModel, width = 220, controlSize = "sm",
                       placeholder = L("panel.route.placeholder"),
                       onChange = function(v) view.routeModel = v end,
                       onSubmit = function(v) view.routeModel = v; dryRun() end }),
            ui.button({ text = L("panel.route.run"), variant = "outline", controlSize = "sm",
                        onClick = function() dryRun() end }),
        }),
        ui.label({ text = view.route and routeSummary(view.route) or L("panel.route.hint"),
                   fontSize = 10, color = "on_surface_variant", maxLines = 4 }),
    }))

    if view.asking then
        table.insert(children, ui.label({ text = L("panel.ask.thinking"), fontSize = 11, color = "primary" }))
    elseif view.answer then
        local a = view.answer
        local meta = a.ok and string.format("%s:%s · %s · %s",
            tostring(a.provider or "-"), tostring(a.model or "-"),
            tostring(a.rule or "-"), hub.fmtMs(a.ms)) or L("panel.ask.failed")
        table.insert(children, ui.column({ fill = "surface_variant", radius = 8, padding = 10, gap = 4 }, {
            ui.row({ gap = 6, align = "center" }, {
                ui.glyph({ name = a.ok and "circle-check" or "alert-triangle", size = 13,
                           color = a.ok and "primary" or "error" }),
                ui.label({ text = meta, fontSize = 10, color = "on_surface_variant", flexGrow = 1,
                           maxLines = 1 }),
                ui.button({ glyph = "copy", variant = "ghost", controlSize = "sm", text = L("panel.ask.copy"),
                            onClick = function()
                                noctalia.copyToClipboard(a.text or "", "text/plain")
                                noctalia.notify("LLM Hub", L("panel.ask.copied"))
                            end }),
            }),
            ui.label({ text = a.text or "", fontSize = 11, color = "on_surface", maxLines = 16 }),
        }))
    end
    return ui.column({ gap = 8, padding = 12 }, children)
end

function routeSummary(r)
    local parts = {}
    for _, c in ipairs(r.candidates or {}) do
        table.insert(parts, string.format("%s:%s", tostring(c.provider), tostring(c.model)))
    end
    return string.format("%s · %s → %s", tostring(r.rule or "-"), tostring(r.tactic or "-"),
        #parts > 0 and table.concat(parts, " → ") or L("panel.route.none"))
end

function dryRun()
    local m = view.routeModel ~= "" and view.routeModel or "*"
    view.route = { rule = L("panel.route.running") }
    render()
    hub.cliJson({ "route", "-m", m }, function(res)
        view.route = res or { rule = L("panel.route.failed") }
        render()
    end)
end

function askNow()
    local prompt = view.askText
    if view.asking or prompt == nil or prompt == "" then return end
    view.asking = true
    view.answer = nil
    render()
    hub.cliJson({ "ask", prompt, "--json", "-m", view.askModel ~= "" and view.askModel or "default" },
        function(res, code)
            view.asking = false
            if type(res) == "table" and res.text ~= nil then
                view.answer = { ok = code == 0, text = res.text, provider = res.provider,
                                model = res.model, rule = res.rule, ms = res.ms }
            else
                view.answer = { ok = false, text = type(res) == "table"
                    and (res.error or res.detail or "") or L("panel.ask.no_response") }
            end
            render()
        end, 60000)
end

-- ── 页签：日志 ──────────────────────────────────────────────────────────────

local function tabLogs()
    local logs = view.logs or {}
    local children = { sectionTitle(L("panel.logs.title", { count = #logs }), "list") }
    if #logs == 0 then
        table.insert(children, ui.label({ text = L("panel.logs.empty"), fontSize = 11,
                                          color = "on_surface_variant" }))
    end
    for _, e in ipairs(logs) do
        local ok = (tonumber(e.status) or 500) < 400
        local ts = e.ts and noctalia.formatTime("%H:%M:%S", e.ts) or "-"
        table.insert(children, ui.row({ gap = 8, align = "center" }, {
            ui.glyph({ name = ok and "circle-check" or "alert-triangle", size = 12,
                       color = ok and "primary" or "error" }),
            ui.label({ text = ts, fontSize = 10, color = "on_surface_variant", width = 62 }),
            ui.label({ text = tostring(e.model or "-"), fontSize = 10, width = 150, maxLines = 1,
                       color = "on_surface" }),
            ui.label({ text = "→ " .. tostring(e.provider or "-"), fontSize = 10, width = 110,
                       maxLines = 1, color = "on_surface_variant" }),
            ui.label({ text = hub.fmtMs(e.ms), fontSize = 10, width = 60, color = "on_surface_variant" }),
            ui.label({ text = string.format("%s+%s", hub.fmtNum(e.tokens_in), hub.fmtNum(e.tokens_out)),
                       fontSize = 10, flexGrow = 1, color = "on_surface_variant", maxLines = 1 }),
        }))
    end
    if view.logsBusy then
        table.insert(children, ui.label({ text = L("panel.logs.loading"), fontSize = 10, color = "primary" }))
    end
    return ui.column({ gap = 6, padding = 12 }, children)
end

-- ── 组装 ────────────────────────────────────────────────────────────────────

local TABS = {
    { id = "overview", label = "panel.tab.overview", glyph = "gauge" },
    { id = "providers", label = "panel.tab.providers", glyph = "server-2" },
    { id = "rules", label = "panel.tab.rules", glyph = "arrows-split-2" },
    { id = "ask", label = "panel.tab.ask", glyph = "messages" },
    { id = "logs", label = "panel.tab.logs", glyph = "list" },
}

local function header()
    local snap = view.snap
    local running = snap and snap.running
    local ep = (snap and snap.endpoint) or hub.endpoint()
    return ui.column({ gap = 8 }, {
        ui.row({ gap = 8, align = "center" }, {
            ui.glyph({ name = running and "server" or "server-2", size = 18,
                       color = running and "primary" or "error" }),
            ui.label({ text = L("panel.title"), fontSize = 16, fontWeight = "bold", flexGrow = 1,
                       color = "on_surface" }),
            ui.label({ text = running and L("panel.running") or L("panel.stopped"), fontSize = 11,
                       color = running and "primary" or "error" }),
        }),
        ui.row({ gap = 8, align = "center" }, {
            ui.glyph({ name = "plug", size = 12, color = "on_surface_variant" }),
            ui.label({ text = ep, fontSize = 11, color = "on_surface_variant", flexGrow = 1, maxLines = 1 }),
            ui.button({ glyph = "copy", variant = "ghost", controlSize = "sm",
                        tooltip = L("panel.copy_endpoint"),
                        onClick = function()
                            noctalia.copyToClipboard(ep, "text/plain")
                            noctalia.notify("LLM Hub", L("panel.copied_endpoint", { endpoint = ep }))
                        end }),
                ui.button({ glyph = "settings", variant = "ghost", controlSize = "sm",
                            tooltip = L("panel.open_settings"),
                            onClick = function() noctalia.openSettings() end }),
        }),
        ui.row({ gap = 8, align = "center" }, {
            ui.button({ text = running and L("panel.stop") or L("panel.start"),
                        glyph = running and "player-stop" or "player-play",
                        variant = running and "outline" or "primary", controlSize = "sm",
                        onClick = function() hub.request(running and "stop" or "start"); refresh() end }),
            ui.button({ text = L("panel.restart"), glyph = "refresh", variant = "outline", controlSize = "sm",
                        onClick = function() hub.request("restart"); refresh() end }),
            ui.button({ text = L("panel.reload_config"), glyph = "reload", variant = "ghost", controlSize = "sm",
                        onClick = function()
                            run(L("panel.reload_config"), { "reload" }, 30000)
                        end }),
            ui.spacer({ flexGrow = 1 }),
            ui.button({ text = L("panel.clear_logs"), glyph = "trash", variant = "ghost", controlSize = "sm",
                        onClick = function() run(L("panel.clear_logs"), { "clear-logs" }, 30000) end }),
            ui.button({ text = L("panel.reset_stats"), glyph = "bolt", variant = "ghost", controlSize = "sm",
                        onClick = function() run(L("panel.reset_stats"), { "reset-stats" }, 30000) end }),
        }),
    })
end

local function tabBar()
    local children = {}
    for _, t in ipairs(TABS) do
        table.insert(children, ui.button({
            key = "tab." .. t.id,
            text = L(t.label),
            glyph = t.glyph,
            variant = view.tab == t.id and "secondary" or "ghost",
            selected = view.tab == t.id,
            controlSize = "sm",
            onClick = function()
                view.tab = t.id
                if t.id == "logs" then fetchLogs() end
                render()
            end,
        }))
    end
    return ui.row({ gap = 4, align = "center" }, children)
end

function render()
    local body
    if view.tab == "providers" then
        body = tabProviders()
    elseif view.tab == "rules" then
        body = tabRules()
    elseif view.tab == "ask" then
        body = tabAsk()
    elseif view.tab == "logs" then
        body = tabLogs()
    else
        body = tabOverview()
    end
    local foot = view.note and ui.label({ text = view.note, fontSize = 10, color = "on_surface_variant",
                                          maxLines = 2 })
        or ui.label({ text = "", fontSize = 1, visible = false })
    panel.render(ui.column({ gap = 8, padding = 12 }, {
        header(),
        tabBar(),
        ui.separator({}),
        ui.scroll({ flexGrow = 1, gap = 8, revealKey = view.tab }, { body }),
        foot,
    }))
end

-- ── 宿主回调 ────────────────────────────────────────────────────────────────

function onOpen(context)
    noctalia.setUpdateInterval(math.max(1000, hub.setting("poll_interval_ms")))
    if not view.watched then
        view.watched = true
        noctalia.state.watch(hub.STATE_KEY, function(snap)
            view.snap = snap
            render()
        end)
    end
    view.snap = hub.state() or view.snap
    render()
    refresh()
    if view.tab == "logs" then fetchLogs() end
end

function onClose()
    view.logsBusy = false
end

function update()
    if view.tab == "logs" then fetchLogs() end
end

function onKey(chord, pressed)
    if not pressed then return end
    if chord == "r" or chord == "ctrl+r" then
        refresh()
        render()
    end
end

function onIpc(event, payload)
    if tostring(event):lower() == "refresh" then
        refresh()
        render()
    end
end
