--!nocheck
-- panel.luau —— 「大模型控制中心」控制台
-- 五个页签：概览 / Provider / 路由规则 / 问答 / 日志
-- 数据来源：service 发布的共享状态快照（hub.state）；动作则直接调 llmhubd CLI。
-- 打开方式：左键点栏图标，或 `noctalia msg panel-toggle ylxc/llm-hub:console`

local hub = require("./lib/hub.luau")

local view = {
    snap = nil,
    tab = "overview",
    editing = nil,       -- 正在编辑的 provider id；nil = 列表视图（整页只放一张卡片，无需滚动）
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
    draft = {},          -- 未保存的输入框内容：{ "url:<id>" = "…", "key:<id>" = …, "models:<id>" = … }
    confirmRemove = nil, -- 两段式删除：先点一次记 id，再点第二次才真删
    ruleEditing = nil,    -- 正在编辑的规则 id；nil = 规则列表视图
    ruleWait = nil,       -- 刚新增的规则 id：等快照刷新到它之前不要退回列表
    confirmRule = nil,    -- 规则的删除确认 id
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
                base_url = p.base_url, path = p.path,
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

local TYPES_FALLBACK = { "openai", "anthropic", "gemini", "ollama", "mock" }

--- 服务商类型候选（由守护进程 /config 给出，离线时用内置默认）
local function providerTypes()
    local cfg = view.snap and view.snap.config
    local t = cfg and cfg.provider_types
    if type(t) == "table" and #t > 0 then return t end
    return TYPES_FALLBACK
end


local function draftOf(kind, id) return view.draft[kind .. ":" .. id] end
local function setDraft(kind, id, text) view.draft[kind .. ":" .. id] = text end

--- 保存一张卡片：只提交真的改动过的字段
local function saveProvider(p)
    local ops = {}
    local url = draftOf("url", p.id)
    if url and url ~= "" and url ~= (p.base_url or "") then
        table.insert(ops, { "providers." .. p.id .. ".base_url", url, false })
    end
    local key = draftOf("key", p.id)
    if key and key ~= "" then
        table.insert(ops, { "providers." .. p.id .. ".api_key", key, false })
    end
    local models = draftOf("models", p.id)
    if models then
        local list = hub.splitList(models)
        -- 空列表写死 "[]"：Luau 里空表无法与空对象区分，交给编码器可能产出 {}。
        local text = "[]"
        if #list > 0 then
            text = hub.jsonEncode(list) or "[]"
        end
        table.insert(ops, { "providers." .. p.id .. ".models", text, true })
    end
    if #ops == 0 then
        view.note = L("panel.providers.nochange", { id = p.id })
        render()
        return
    end
    view.note = L("panel.busy", { label = L("panel.providers.save") })
    render()
    hub.runOps(ops, function(_, failN)
        if failN > 0 then
            view.note = L("panel.failed", { label = L("panel.providers.save"), detail = failN })
        else
            view.note = L("panel.providers.saved", { id = p.id })
            view.draft["url:" .. p.id] = nil
            view.draft["key:" .. p.id] = nil
            view.draft["models:" .. p.id] = nil
        end
        refresh()
        render()
    end)
end

--- 换类型：先把地址/路径清空，让守护进程按新类型的内置模板重填
local function setType(p, newType)
    if newType == p.type then return end
    view.note = L("panel.busy", { label = L("panel.providers.type") })
    render()
    hub.runOps({
        { "providers." .. p.id .. ".base_url", "", false },
        { "providers." .. p.id .. ".path", "", false },
        { "providers." .. p.id .. ".type", newType, false },
    }, function(_, failN)
        if failN > 0 then
            view.note = L("panel.failed", { label = L("panel.providers.type"), detail = failN })
        else
            view.note = L("panel.providers.type_changed", { type = newType })
        end
        view.draft["url:" .. p.id] = nil
        refresh()
        render()
    end)
end

local function addProvider()
    view.note = L("panel.busy", { label = L("panel.providers.add") })
    render()
    hub.providerAdd({ type = "openai" }, function(res, code)
        if code == 0 and res and res.id then
            view.note = L("panel.providers.added", { id = res.id })
            view.editing = res.id -- 新增后直接进编辑视图，方便接着填地址/密钥/模型
        else
            view.note = L("panel.failed", { label = L("panel.providers.add"), detail = code })
        end
        refresh()
        render()
    end)
end

--- 删除成功后退出编辑视图
local function leaveEditor()
    view.editing = nil
    view.confirmRemove = nil
end

--- 两段式删除：第一次点只是记住 id，第二次点才真删
local function removeProvider(p)
    if view.confirmRemove ~= p.id then
        view.confirmRemove = p.id
        view.note = L("panel.providers.remove_ask", { id = p.id })
        render()
        return
    end
    view.confirmRemove = nil
    hub.providerRemove(p.id, function(_, code)
        if code == 0 then
            view.note = L("panel.providers.removed", { id = p.id })
            leaveEditor()
        else
            view.note = L("panel.failed", { label = L("panel.providers.remove"), detail = code })
        end
        refresh()
        render()
    end)
end

--- 把测试结果整理成一行摘要 + 正文：成功显示 model/延迟/tokens 与答复，
--- 失败显示上游原话（kind: error），实在没 JSON 才回落兜底文案。
local function testText(res, code, raw)
    if type(res) == "table" then
        if res.ok and res.text then
            return string.format("%s · %sms · %d+%d tokens\n%s",
                tostring(res.model or ""), tostring(res.ms or ""),
                tonumber(res.tokens_in) or 0, tonumber(res.tokens_out) or 0,
                tostring(res.text))
        end
        if not res.ok then
            return string.format("%s: %s", tostring(res.kind or "error"),
                tostring(res.error or L("panel.providers.test_fail")))
        end
    end
    -- 没有 JSON（老版本 CLI / 纯文本输出）：用原始输出的第一行，别一律说「无法连接」
    local first = (raw or ""):match("^%s*([^\n]*)")
    if first and first ~= "" then return first end
    if code ~= 0 then return string.format("exit %s", tostring(code)) end
    return L("panel.providers.test_fail")
end

local function testProvider(p)
    view.test = { id = p.id, ok = nil }
    render()
    hub.cliJson({ "test", p.id, "--json" }, function(res, code, raw)
        local okFlag = code == 0
        if type(res) == "table" and res.ok ~= nil then okFlag = res.ok and true or false end
        view.test = { id = p.id, ok = okFlag, text = testText(res, code, raw) }
        render()
    end, 60000)
end

local function fieldRow(label, node)
    return ui.row({ gap = 8, align = "center" }, {
        ui.label({ text = label, fontSize = 11, color = "on_surface_variant",
                   width = 58, maxLines = 1 }),
        node,
    })
end

--- 一行紧凑摘要（列表视图用，不含输入框：内容短、一屏能看全，也避免输入框吞掉滚轮）
local function providerRow(p)
    local status = p.enabled and L("panel.providers.on") or L("panel.providers.off")
    local color = p.enabled and "primary" or "on_surface_variant"
    local detail = string.format("%s · %s · %d %s", p.id, status, p.models_n, L("panel.unit.models"))
    if p.missing_env and #p.missing_env > 0 then
        detail = detail .. " · " .. L("panel.providers.missing_env") .. " " .. table.concat(p.missing_env, ",")
    end
    if p.models_n == 0 then
        detail = detail .. " · " .. L("panel.providers.idle")
    elseif not p.has_key and p.type ~= "mock" and p.type ~= "ollama" then
        detail = detail .. " · " .. L("panel.providers.no_key")
    end
    return ui.row({ key = "prow:" .. p.id, gap = 8, align = "center",
                    fill = "surface_variant", radius = 8, padding = 8 }, {
        ui.glyph({ name = p.enabled and "plug-connected" or "plug", size = 14, color = color }),
        ui.column({ gap = 1, flexGrow = 1 }, {
            ui.label({ text = p.label, fontSize = 12, fontWeight = "medium",
                       color = "on_surface", maxLines = 1 }),
            ui.label({ text = detail, fontSize = 10, color = "on_surface_variant", maxLines = 1 }),
        }),
        ui.toggle({ checked = p.enabled,
                    onChange = function(v) setEnabled(p, v == "true") end }),
        ui.button({ glyph = "pencil", controlSize = "sm", variant = "ghost",
                    tooltip = L("panel.providers.edit"),
                    onClick = function() view.editing = p.id; render() end }),
    })
end

--- 一张可编辑的服务商卡片：类型 / 地址 / 密钥 / 模型 + 测试·保存·删除
local function providerCard(p)
    local status = p.enabled and L("panel.providers.on") or L("panel.providers.off")
    local color = p.enabled and "primary" or "on_surface_variant"
    local detail = string.format("%s · %s · %d %s", p.id, status, p.models_n,
                                 L("panel.unit.models"))
    if p.missing_env and #p.missing_env > 0 then
        detail = detail .. " · " .. L("panel.providers.missing_env") ..
                 " " .. table.concat(p.missing_env, ",")
    end
    if p.models_n == 0 then
        detail = detail .. " · " .. L("panel.providers.idle")
    elseif not p.has_key and p.type ~= "mock" and p.type ~= "ollama" then
        detail = detail .. " · " .. L("panel.providers.no_key")
    end
    local stats = string.format("%s · %s", hub.fmtInt(p.requests or 0), hub.fmtMs(p.latency_ms))
    if (p.errors or 0) > 0 then
        stats = stats .. " · ✗ " .. tostring(p.errors)
    end

    local typeButtons = {}
    for _, name in ipairs(providerTypes()) do
        table.insert(typeButtons, ui.button({
            text = name, controlSize = "sm",
            variant = (name == p.type) and "primary" or "outline",
            selected = (name == p.type),
            tooltip = L("panel.providers.type_hint"),
            onClick = function() setType(p, name) end,
        }))
    end

    local urlInput = ui.input({
        key = "url:" .. p.id,
        value = draftOf("url", p.id) or p.base_url or "",
        placeholder = L("panel.providers.url_hint"),
        controlSize = "sm", flexGrow = 1, submitOnEnter = true,
        onChange = function(v) setDraft("url", p.id, v) end,
        onSubmit = function(v) setDraft("url", p.id, v); saveProvider(p) end,
    })
    local keyInput = ui.input({
        key = "key:" .. p.id,
        value = draftOf("key", p.id) or "",
        -- password 字段连 placeholder 也会被渲染成圆点，所以这里不给 placeholder，改用下方提示行。
        placeholder = "",
        password = true, controlSize = "sm", flexGrow = 1, submitOnEnter = true,
        onChange = function(v) setDraft("key", p.id, v) end,
        onSubmit = function(v) setDraft("key", p.id, v); saveProvider(p) end,
    })
    local modelsInput = ui.input({
        key = "models:" .. p.id,
        value = draftOf("models", p.id) or table.concat(p.models or {}, ", "),
        placeholder = L("panel.providers.models_hint"),
        controlSize = "sm", flexGrow = 1, submitOnEnter = true,
        onChange = function(v) setDraft("models", p.id, v) end,
        onSubmit = function(v) setDraft("models", p.id, v); saveProvider(p) end,
    })

    local confirming = view.confirmRemove == p.id
    return ui.column({ key = "card:" .. p.id, fill = "surface_variant", radius = 8,
                       padding = 10, gap = 6 }, {
        ui.row({ gap = 8, align = "center" }, {
            ui.glyph({ name = p.enabled and "plug-connected" or "plug", size = 14, color = color }),
            ui.column({ gap = 1, flexGrow = 1 }, {
                ui.label({ text = p.label, fontSize = 12, fontWeight = "medium",
                           color = "on_surface", maxLines = 1 }),
                ui.label({ text = detail, fontSize = 10, color = "on_surface_variant",
                           maxLines = 1 }),
            }),
            ui.toggle({ checked = p.enabled,
                        onChange = function(v) setEnabled(p, v == "true") end }),
        }),
        fieldRow(L("panel.providers.type"), ui.row({ gap = 4, align = "center" }, typeButtons)),
        fieldRow(L("panel.providers.url"), urlInput),
        ui.column({ gap = 2 }, {
            fieldRow(L("panel.providers.key"), keyInput),
            ui.label({ text = p.has_key and L("panel.providers.key_hint_saved")
                                          or L("panel.providers.key_hint_new"),
                       fontSize = 9, color = "on_surface_variant", maxLines = 1 }),
        }),
        fieldRow(L("panel.providers.models"), modelsInput),
        ui.row({ gap = 6, align = "center" }, {
            ui.label({ text = stats, fontSize = 10, color = "on_surface_variant",
                       flexGrow = 1, maxLines = 1 }),
            ui.button({ text = L("panel.providers.test"), glyph = "wand", variant = "ghost",
                        controlSize = "sm", onClick = function() testProvider(p) end }),
            ui.button({ text = L("panel.providers.save"), glyph = "check", variant = "primary",
                        controlSize = "sm", onClick = function() saveProvider(p) end }),
            ui.button({ text = confirming and L("panel.providers.remove_confirm")
                                         or L("panel.providers.remove"),
                        glyph = "trash", controlSize = "sm",
                        variant = confirming and "destructive" or "ghost",
                        onClick = function() removeProvider(p) end }),
        }),
        ui.label({ text = p.last_error or "", fontSize = 10, color = "error", maxLines = 1,
                   visible = (p.last_error ~= nil and p.last_error ~= "") }),
    })
end

--- 测试结果卡片（列表/编辑两个视图都可能出现在底部）
local function testBlock()
    if not view.test then return nil end
    return ui.column({ fill = "surface_variant", radius = 8, padding = 8, gap = 2 }, {
        ui.label({ text = L("panel.providers.test_result", { id = view.test.id }), fontSize = 11,
                   fontWeight = "medium", color = view.test.ok and "primary" or "error" }),
        ui.label({ text = (view.test.text or ""):sub(1, 300), fontSize = 10, maxLines = 3,
                   color = "on_surface_variant" }),
    })
end

--- 服务商页签：列表（紧凑行，一眼看全）⇄ 编辑（只放一张卡片，不需要滚动）
local function tabProviders()
    local list = providerList()

    -- ── 编辑视图：整页只放一张卡片
    if view.editing then
        for _, p in ipairs(list) do
            if p.id == view.editing then
                local kids = {
                    ui.row({ gap = 8, align = "center" }, {
                        ui.button({ glyph = "arrow-left", controlSize = "sm", variant = "ghost",
                                    tooltip = L("panel.providers.back"),
                                    onClick = function()
                                        view.editing = nil
                                        view.confirmRemove = nil
                                        render()
                                    end }),
                        ui.label({ text = L("panel.providers.edit_title", { id = p.id }), fontSize = 13,
                                   fontWeight = "medium", color = "on_surface", flexGrow = 1,
                                   maxLines = 1 }),
                    }),
                    providerCard(p),
                }
                local tb = testBlock()
                if tb then table.insert(kids, tb) end
                return ui.column({ gap = 8, padding = 12 }, kids)
            end
        end
        -- 正在编辑的 provider 已被删掉：退回列表
        view.editing = nil
    end

    -- ── 列表视图
    local children = { sectionTitle(L("panel.providers.title", { count = #list }), "server-2") }
    table.insert(children, ui.label({ text = L("panel.providers.hint"), fontSize = 10,
                                      color = "on_surface_variant", maxLines = 2, height = 30 }))
    if #list == 0 then
        table.insert(children, ui.label({ text = L("panel.providers.empty"), fontSize = 11,
                                          color = "on_surface_variant" }))
    end
    for _, p in ipairs(list) do
        table.insert(children, providerRow(p))
    end
    local tb = testBlock()
    if tb then table.insert(children, tb) end
    table.insert(children, ui.row({ gap = 8, align = "center" }, {
        ui.button({ text = L("panel.providers.add"), glyph = "plus", variant = "primary",
                    controlSize = "md", onClick = function() addProvider() end }),
        ui.label({ text = L("panel.providers.add_hint"), fontSize = 10, flexGrow = 1,
                   color = "on_surface_variant" }),
    }))
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

-- ── 路由规则：列表 ⇄ 编辑 两级视图 ───────────────────────────────────

local TACTICS = { "listed", "priority", "cheapest", "fastest", "least_used", "random", "round_robin" }

--- 规则列表（来自配置快照）
local function ruleList()
    local cfg = (view.snap and view.snap.config) or {}
    return cfg.rules or {}
end

local function ruleOf(id)
    for _, r in ipairs(ruleList()) do
        if r.id == id then return r end
    end
    return nil
end

--- 按点分路径取规则里的字段值
local function fieldValue(r, path)
    local cur = r
    for seg in path:gmatch("[^.]+") do
        if type(cur) ~= "table" then return nil end
        cur = cur[seg]
    end
    return cur
end

local function fieldText(value)
    if type(value) == "table" then return table.concat(value, ", ") end
    if value == nil then return "" end
    return tostring(value)
end

--- 编辑表单的文本字段：{草稿前缀, 配置路径, 翻译键, 是否多值}
local RULE_FIELDS = {
    { "rdesc", "description", "f_desc", false },
    { "rmodel", "when.model", "f_model", true },
    { "rprovider", "when.provider", "f_provider", true },
    { "rtags", "when.tags", "f_tags", true },
    { "rscenario", "when.scenario", "f_scenario", true },
    { "rmintok", "when.min_prompt_tokens", "f_mintok", false },
    { "rmaxtok", "when.max_prompt_tokens", "f_maxtok", false },
    { "rregex", "when.prompt_regex", "f_regex", false },
    { "rchain", "route.providers", "f_chain", true },
}


--- 统一的收尾：写提示 + 刷新快照 + 重绘
local function ruleDone(label, code, okNote)
    if code ~= 0 then
        view.note = L("panel.failed", { label = label, detail = code })
    else
        view.note = okNote
    end
    refresh()
    render()
end

local function setRuleEnabled(r, want)
    local label = L("panel.rules.toggle")
    view.note = L("panel.busy", { label = label })
    render()
    hub.setPath("rules." .. r.id .. ".enabled", want and "true" or "false", function(_, code)
        ruleDone(label, code,
                 want and L("panel.rules.now_on", { id = r.id }) or L("panel.rules.now_off", { id = r.id }))
    end)
end

local function setRuleFallback(r, want)
    local label = L("panel.rules.f_fallback")
    view.note = L("panel.busy", { label = label })
    render()
    hub.setPath("rules." .. r.id .. ".route.fallback", want and "true" or "false", function(_, code)
        ruleDone(label, code, L("panel.rules.saved", { id = r.id }))
    end)
end

--- 工具要求：0 不限（删掉该条件）/ 1 必须带工具 / 2 必须不带工具
local function setRuleTools(r, idx)
    local label = L("panel.rules.f_tools")
    local key = "rules." .. r.id .. ".when.has_tools"
    view.note = L("panel.busy", { label = label })
    render()
    local function done(_, code) ruleDone(label, code, L("panel.rules.saved", { id = r.id })) end
    if idx == 0 then
        hub.unsetPath(key, done)
    elseif idx == 1 then
        hub.setPath(key, "true", done)
    else
        hub.setPath(key, "false", done)
    end
end

local function setRuleTactic(r, idx)
    local name = TACTICS[idx + 1]
    if not name then return end
    local label = L("panel.rules.f_tactic")
    view.note = L("panel.busy", { label = label })
    render()
    hub.setPath("rules." .. r.id .. ".route.tactic", name, function(_, code)
        ruleDone(label, code, L("panel.rules.saved", { id = r.id }))
    end)
end

local function moveRule(r, dir)
    local label = L("panel.rules.move")
    view.note = L("panel.busy", { label = label })
    render()
    hub.ruleMove(r.id, dir, function(res, code)
        if code ~= 0 or (type(res) == "table" and res.moved == false) then
            ruleDone(label, 1, nil)
            view.note = L("panel.rules.move_edge", { id = r.id })
            render()
        else
            ruleDone(label, 0, L("panel.rules.moved", { id = r.id }))
        end
    end)
end

local function addRule()
    local label = L("panel.rules.add")
    view.note = L("panel.busy", { label = label })
    render()
    hub.ruleAdd({ disabled = true }, function(res, code)   -- 新规则先保持停用，填好条件再打开开关
        if code == 0 and type(res) == "table" and res.id then
            view.ruleEditing = res.id
            view.ruleWait = res.id
            view.note = L("panel.rules.added", { id = res.id })
        else
            view.note = L("panel.failed", { label = label, detail = code })
        end
        refresh()
        render()
    end)
end

local function removeRule(r)
    if view.confirmRule ~= r.id then
        view.confirmRule = r.id
        view.note = L("panel.rules.remove_ask", { id = r.id })
        render()
        return
    end
    view.confirmRule = nil
    local label = L("panel.rules.remove")
    hub.ruleRemove(r.id, function(_, code)
        if code == 0 then
            view.ruleEditing = nil
            for k in pairs(view.draft) do
                if k:sub(-(#r.id + 1)) == ":" .. r.id then view.draft[k] = nil end
            end
        end
        ruleDone(label, code, L("panel.rules.removed", { id = r.id }))
    end)
end

--- 保存编辑视图里的文本字段：只提交与现值不同的字段，清空 = 删除该条件
local function saveRule(r)
    local ops = {}
    for _, f in ipairs(RULE_FIELDS) do
        local kind, path, isList = f[1], f[2], f[4]
        local text = draftOf(kind, r.id)
        if text ~= nil and text ~= fieldText(fieldValue(r, path)) then
            local full = "rules." .. r.id .. "." .. path
            local list = hub.splitList(text)
            if text == "" or #list == 0 then
                table.insert(ops, { full, "null", true })
            elseif isList then
                table.insert(ops, { full, hub.jsonEncode(list) or "[]", true })
            elseif path:find("tokens") and tonumber(text) then
                table.insert(ops, { full, tostring(math.floor(tonumber(text))), true })
            else
                table.insert(ops, { full, text, false })
            end
        end
    end
    if #ops == 0 then
        view.note = L("panel.rules.nochange", { id = r.id })
        render()
        return
    end
    local label = L("panel.rules.save")
    view.note = L("panel.busy", { label = label })
    render()
    hub.runOps(ops, function(_, failed)
        if failed and failed > 0 then
            ruleDone(label, failed, nil)
            return
        end
        for _, f in ipairs(RULE_FIELDS) do view.draft[f[1] .. ":" .. r.id] = nil end
        ruleDone(label, 0, L("panel.rules.saved", { id = r.id }))
    end)
end

--- 列表视图的一行：名称 + 条件摘要 + 上移/下移 + 启停 + 编辑
local function ruleRow(r)
    local route = r.route or r
    local enabled = r.enabled ~= false
    local chain = #(route.providers or {}) > 0 and table.concat(route.providers, " → ") or "*"
    return ui.row({ key = "rrow:" .. r.id, gap = 8, align = "center",
                    fill = "surface_variant", radius = 8, padding = 8 }, {
        ui.glyph({ name = enabled and "bolt" or "bolt-off", size = 14,
                   color = enabled and "primary" or "on_surface_variant" }),
        ui.column({ gap = 1, flexGrow = 1 }, {
            ui.label({ text = r.id, fontSize = 12, fontWeight = "medium", color = "on_surface", maxLines = 1 }),
            ui.label({ text = string.format("%s · %s · → %s", route.tactic or "priority",
                       whenSummary(r.when), chain),
                       fontSize = 10, color = "on_surface_variant", maxLines = 1 }),
        }),
        ui.button({ glyph = "arrow-up", controlSize = "sm", variant = "ghost",
                    tooltip = L("panel.rules.move_up"), onClick = function() moveRule(r, "up") end }),
        ui.button({ glyph = "arrow-down", controlSize = "sm", variant = "ghost",
                    tooltip = L("panel.rules.move_down"), onClick = function() moveRule(r, "down") end }),
        ui.toggle({ checked = enabled, onChange = function(v) setRuleEnabled(r, v == "true") end }),
        ui.button({ glyph = "pencil", controlSize = "sm", variant = "ghost",
                    tooltip = L("panel.rules.edit"),
                    onClick = function()
                        view.ruleEditing = r.id
                        view.confirmRule = nil
                        view.note = nil
                        render()
                    end }),
    })
end

--- 一行文本字段：标签在上、输入框在下（中文长标签放不进 fieldRow 的固定宽度）
local function ruleField(r, kind, path, labelKey, isList)
    local stored = draftOf(kind, r.id)
    local value = stored or fieldText(fieldValue(r, path))
    local hint = ""
    if kind == "rmodel" then hint = L("panel.rules.model_hint")
    elseif kind == "rchain" then hint = L("panel.rules.chain_hint") end
    return ui.column({ gap = 2, flexGrow = 1 }, {
        ui.label({ text = L("panel.rules." .. labelKey), fontSize = 10,
                   color = "on_surface_variant", maxLines = 1 }),
        ui.input({ key = kind .. ":" .. r.id, value = value, placeholder = hint,
                   controlSize = "sm", flexGrow = 1, submitOnEnter = true,
                   onChange = function(v) setDraft(kind, r.id, v) end,
                   onSubmit = function(v) setDraft(kind, r.id, v); saveRule(r) end }),
    })
end

--- 编辑视图：一张表单卡片
local function ruleCard(r)
    local route = r.route or r
    local kids = {
        ui.row({ gap = 8, align = "center" }, {
            ui.glyph({ name = "bolt", size = 14, color = "primary" }),
            ui.label({ text = r.id, fontSize = 12, fontWeight = "medium", flexGrow = 1,
                       color = "on_surface", maxLines = 1 }),
            ui.toggle({ checked = r.enabled ~= false,
                        onChange = function(v) setRuleEnabled(r, v == "true") end }),
        }),
        ui.label({ text = L("panel.rules.edit_hint"), fontSize = 10,
                   color = "on_surface_variant", maxLines = 2, height = 30 }),
    }
    local function fld(i)
        local f = RULE_FIELDS[i]
        return ruleField(r, f[1], f[2], f[3], f[4])
    end
    local function pair(i, j) return ui.row({ gap = 8, align = "start" }, { fld(i), fld(j) }) end
    table.insert(kids, pair(1, 2))   -- 描述 / 模型匹配
    table.insert(kids, pair(3, 4))   -- 服务商匹配 / 标签匹配
    table.insert(kids, pair(5, 8))   -- 场景匹配 / 提示词正则
    table.insert(kids, pair(6, 7))   -- 最小 / 最大 prompt tokens
    table.insert(kids, fld(9))       -- 候选链（整行）
    -- 工具要求 / 排序策略（并排）
    local toolsIdx = 0
    if r.when and r.when.has_tools == true then toolsIdx = 1
    elseif r.when and r.when.has_tools == false then toolsIdx = 2 end
    local tacticIdx = 0
    local labels = {}
    for i, t in ipairs(TACTICS) do
        table.insert(labels, L("panel.rules.tactic_" .. t))
        if t == (route.tactic or "priority") then tacticIdx = i - 1 end
    end
    table.insert(kids, ui.row({ gap = 8, align = "start" }, {
        ui.column({ gap = 2, flexGrow = 1 }, {
            ui.label({ text = L("panel.rules.f_tools"), fontSize = 10, color = "on_surface_variant" }),
            ui.select({ key = "rtools:" .. r.id, controlSize = "sm", flexGrow = 1, selectedIndex = toolsIdx,
                        options = { L("panel.rules.tools_any"), L("panel.rules.tools_yes"), L("panel.rules.tools_no") },
                        onChange = function(idx) setRuleTools(r, tonumber(idx) or 0) end }),
        }),
        ui.column({ gap = 2, flexGrow = 1 }, {
            ui.label({ text = L("panel.rules.f_tactic"), fontSize = 10, color = "on_surface_variant" }),
            ui.select({ key = "rtactic:" .. r.id, controlSize = "sm", flexGrow = 1, selectedIndex = tacticIdx,
                        options = labels,
                        onChange = function(idx) setRuleTactic(r, tonumber(idx) or 0) end }),
        }),
    }))
    -- 失败回退
    table.insert(kids, ui.row({ gap = 8, align = "center" }, {
        ui.toggle({ checked = route.fallback ~= false,
                    onChange = function(v) setRuleFallback(r, v == "true") end }),
        ui.label({ text = L("panel.rules.f_fallback"), fontSize = 10, flexGrow = 1,
                   color = "on_surface_variant", maxLines = 1 }),
    }))
    -- 保存 / 删除
    table.insert(kids, ui.row({ gap = 8, align = "center" }, {
        ui.button({ text = L("panel.rules.save"), glyph = "check", variant = "primary",
                    controlSize = "md", onClick = function() saveRule(r) end }),
        ui.button({ text = view.confirmRule == r.id and L("panel.rules.remove_confirm") or L("panel.rules.remove"),
                    glyph = "trash", variant = view.confirmRule == r.id and "destructive" or "ghost",
                    controlSize = "md", onClick = function() removeRule(r) end }),
    }))
    return ui.column({ fill = "surface_variant", radius = 8, padding = 10, gap = 6 }, kids)
end

local function tabRules()
    local cfg = (view.snap and view.snap.config) or {}

    -- 编辑视图
    if view.ruleEditing then
        local r = ruleOf(view.ruleEditing)
        if r then
            view.ruleWait = nil
            return ui.column({ gap = 8, padding = 12 }, {
                ui.row({ gap = 8, align = "center" }, {
                    ui.button({ glyph = "arrow-left", controlSize = "sm", variant = "ghost",
                                tooltip = L("panel.rules.back"),
                                onClick = function()
                                    view.ruleEditing = nil
                                    view.confirmRule = nil
                                    render()
                                end }),
                    ui.label({ text = L("panel.rules.edit_title", { id = r.id }), fontSize = 13,
                               fontWeight = "medium", flexGrow = 1, color = "on_surface", maxLines = 1 }),
                    ui.button({ glyph = "arrow-narrow-up", controlSize = "sm", variant = "ghost",
                                tooltip = L("panel.rules.move_up"), onClick = function() moveRule(r, "up") end }),
                    ui.button({ glyph = "arrow-narrow-down", controlSize = "sm", variant = "ghost",
                                tooltip = L("panel.rules.move_down"), onClick = function() moveRule(r, "down") end }),
                }),
                ruleCard(r),
            })
        end
        if view.ruleWait == view.ruleEditing then
            -- 刚新增的规则：等快照刷新到它（refresh 的渲染会进编辑器），期间不要退回列表
            return ui.column({ gap = 8, padding = 12 }, {
                ui.row({ gap = 8, align = "center" }, {
                    ui.button({ glyph = "arrow-left", controlSize = "sm", variant = "ghost",
                                tooltip = L("panel.rules.back"),
                                onClick = function()
                                    view.ruleEditing, view.ruleWait = nil, nil
                                    render()
                                end }),
                    ui.label({ text = L("panel.rules.edit_title", { id = view.ruleEditing }), fontSize = 13,
                               fontWeight = "medium", flexGrow = 1, color = "on_surface", maxLines = 1 }),
                }),
                ui.label({ text = L("panel.loading"), fontSize = 11, color = "on_surface_variant" }),
            })
        end
        view.ruleEditing = nil   -- 规则已被删掉，退回列表
    end

    -- 列表视图
    local children = { sectionTitle(L("panel.rules.title"), "arrows-split-2") }
    table.insert(children, ui.label({ text = L("panel.rules.hint"), fontSize = 10,
                                      color = "on_surface_variant", maxLines = 2, height = 30 }))
    local rules = cfg.rules or {}
    if #rules == 0 then
        table.insert(children, ui.label({ text = L("panel.rules.empty"), fontSize = 11,
                                          color = "on_surface_variant" }))
    end
    for _, r in ipairs(rules) do
        table.insert(children, ruleRow(r))
    end
    table.insert(children, ui.row({ gap = 8, align = "center", padding = 4 }, {
        ui.button({ text = L("panel.rules.add"), glyph = "plus", variant = "primary",
                    controlSize = "md", onClick = function() addRule() end }),
        ui.label({ text = L("panel.rules.add_hint"), fontSize = 10, flexGrow = 1,
                   color = "on_surface_variant", maxLines = 2 }),
    }))

    -- 默认路由（无规则命中时的兜底）
    local d = cfg.default_route or {}
    table.insert(children, ui.column({ fill = "surface_variant", radius = 8, padding = 8, gap = 3 }, {
        ui.row({ gap = 8, align = "center" }, {
            ui.glyph({ name = "route", size = 13, color = "primary" }),
            ui.label({ text = L("panel.rules.default"), fontSize = 12, fontWeight = "medium",
                       flexGrow = 1, color = "on_surface", maxLines = 1 }),
            ui.label({ text = d.tactic or "priority", fontSize = 10, color = "primary" }),
        }),
        ui.label({ text = "→ " .. (d.providers and #d.providers > 0 and table.concat(d.providers, " → ") or "*"),
                   fontSize = 10, color = "on_surface", maxLines = 2 }),
        ui.label({ text = L("panel.rules.fallback_on"), fontSize = 10, color = "on_surface_variant",
                   visible = d.fallback == true, maxLines = 1 }),
    }))

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
--- 按 id 跑一次连通性测试（id 为空 = 第一个 provider）。
--- 供 onOpen / onIpc 使用：面板还没拿到快照时先补一次快照再跑。
local function runTestById(id)
    if id == "" then id = nil end
    local function pick()
        local list = (view.snap and view.snap.config and view.snap.config.providers) or {}
        local target = nil
        for _, p in ipairs(list) do
            if id == nil or p.id == id then target = target or p end
        end
        if target then
            testProvider(target)
        else
            view.test = { id = tostring(id or "?"), ok = false,
                          text = L("panel.providers.empty") }
            render()
        end
    end
    if view.snap and view.snap.config and (view.snap.config.providers or {})[1] then
        pick()
    else
        hub.snapshot({}, function(snap) view.snap = snap; pick() end)
    end
end

--- 打开面板时的上下文：让脚本/栏按钮直接定位到某一页，
---   noctalia msg panel-open ylxc/llm-hub:console providers
---   noctalia msg panel-open ylxc/llm-hub:console test:deepseek
local function applyContext(ctx)
    if type(ctx) ~= "string" or ctx == "" then return end
    local cmd, arg = ctx:match("^%s*([%w_%-]+)%s*[:=]?%s*(%S*)")
    cmd = (cmd or ""):lower()
    if cmd == "providers" or cmd == "provider" then
        view.tab = "providers"
        view.editing = (arg ~= "" and arg) or nil
        render()
    elseif cmd == "test" then
        view.tab = "providers"
        runTestById(arg)
    elseif cmd == "logs" then
        view.tab = "logs"
        fetchLogs()
    elseif cmd == "rules" then
        view.tab = "rules"
        view.ruleEditing = nil
        view.ruleWait = nil
        render()
    elseif cmd == "rule" then
        view.tab = "rules"
        view.ruleEditing = (arg ~= "" and arg) or nil
        view.ruleWait = nil
        render()
    elseif cmd == "overview" or cmd == "ask" then
        view.tab = cmd
        render()
    end
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
    applyContext(context)
end

function onClose()
    view.logsBusy = false
end
function update()
    if view.tab == "logs" then fetchLogs() end
    -- 快照过期就自己拉一次：service 卡住／没在跑时，面板依旧能自我更新。
    local now = noctalia.nowMs()
    if view.fetching then
        if now - (view.fetchAt or 0) < 20000 then return end
        view.fetching = false
    end
    if view.fetchedAt and now - view.fetchedAt < 6000 then return end
    view.fetching = true
    view.fetchAt = now
    hub.snapshot({ logs = 25 }, function(snap)
        view.fetching = false
        view.fetchedAt = noctalia.nowMs()
        view.snap = snap
        hub.publish(snap)
        render()
    end)
end

function onKey(chord, pressed)
    if not pressed then return end
    if chord == "r" or chord == "ctrl+r" then
        refresh()
        render()
    end
end

function onIpc(event, payload)
    local ev = tostring(event or ""):lower()
    if ev == "refresh" then
        refresh()
        render()
    elseif ev == "test" then
        -- noctalia msg plugin ylxc/llm-hub:console <target> test deepseek
        view.tab = "providers"
        runTestById(type(payload) == "string" and payload or nil)
    end
end
