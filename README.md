# LLM Hub · 大模型控制中心

把**任意数量**的大模型 API 收进一个端点，用**规则**决定每次请求走谁，再通过**本地 HTTP 接口 + CLI**给你机器上的任何程序调用。
既是 Noctalia 桌面插件（状态栏图标 + 控制台面板 + 快捷键），也是一个能脱离 Noctalia 独立运行的网关。

```
你的应用 ──▶ http://127.0.0.1:8787/v1/chat/completions ──▶ 规则路由 ──▶ DeepSeek / OpenAI / Claude / Gemini / Ollama / 自建 vLLM …
```

- **任意数量 provider**：内置 `openai` / `anthropic` / `gemini` / `ollama` / `mock` 五种适配器，任何 OpenAI 兼容端点（DeepSeek、Moonshot、通义、OpenRouter、vLLM、SGLang、LM Studio、LocalAI、Jan…）都能直接接。
- **规则自动路由**：按模型名、是否带工具、是否带图、prompt 长度、协议、标签、路径正则等条件匹配规则，规则内可用 `priority` / `cheapest` / `fastest` / `least_used` / `round_robin` / `random` / `listed` 七种挑选策略，并带自动降级（fallback）与重试。
- **协议双向翻译**：入站 `OpenAI /chat/completions`、`Anthropic /v1/messages`、`OpenAI /v1/responses` 三种协议任意进、任意出；OpenAI 请求打到 Anthropic 上游、Claude Code 打到 Gemini 上游都可以（含 SSE 流式、工具调用、多模态图片、用量统计）。
- **面板里改 provider**：控制台 `Provider` 页签两级视图（列表 ⇄ 编辑），可直接改类型、地址、密钥、模型，也能开关、连通性测试、新增/删除服务商，改完即时热重载，不用手改 JSON。
- **面板里改路由规则**：控制台 `路由规则` 页签同样是列表 ⇄ 编辑两级视图——一行一条规则，右侧开关直接启停、`↑`/`↓` 调优先级（越靠前越先命中）、铅笔进编辑器改条件与候选链，底部「+ 添加规则」、编辑器内可删除规则，全程不用手改 JSON。
- **思考内容透传**：DeepSeek 的 `reasoning_content`、Claude 的 `thinking` 块、Gemini 的 `thought` 都会归一成 `reasoning` 并按客户端协议原样输出（非流式与流式都支持）。
- **本地接口**：默认在 `localhost:8787` 上同时监听 `127.0.0.1` 与 `::1`（IPv4/IPv6 都能连），OpenAI 兼容，任何 SDK 改一行 `base_url` 即可接入；另有 `/admin/ask` 这种「一句话拿文本」的最简接口。
- **CLI**：`llmhubd start/stop/status/ask/route/models/stats/logs/test/…`，不需要写代码就能用；`bin/llmhub` 是免安装入口。
- **零依赖**：网关只用 Python 标准库（3.9+），不需要 pip 安装任何东西，也不需要 Node。
- **用量与观测**：每个 provider 的请求数/错误数/延迟 EWMA/tokens/成本估算、按模型与按规则分组、请求日志（可清空/重置）、`X-LLMHub-*` 响应头标注本次实际命中的 provider/模型/规则/策略/耗时。

---

## 目录

- [快速开始](#快速开始)
- [本地接口](#本地接口)
- [配置](#配置)
- [在控制台面板里改规则](#在控制台面板里改规则)
- [路由规则](#路由规则)
- [Provider 适配器](#provider-适配器)
- [协议翻译](#协议翻译)
- [CLI 参考](#cli-参考)
- [Noctalia 插件](#noctalia-插件)
- [目录结构](#目录结构)
- [测试](#测试)
- [安全](#安全)
- [常见问题](#常见问题)

---

## 快速开始

### 方式 A：只当本地网关用（不需要 Noctalia）

```bash
cd ~/.local/share/noctalia/plugins/ylxc-llm-hub

./bin/llmhub init          # 写出配置模板 ~/.config/llm-hub/config.json
./bin/llmhub doctor        # 环境自检（python 版本、端口、配置文件、provider）
./bin/llmhub start         # 后台启动网关（默认 127.0.0.1:8787）
./bin/llmhub status        # 看状态与 provider 概览

./bin/llmhub ask "用一句话解释什么是网关"          # 最简调用（自动选 provider）
./bin/llmhub ask -m mock-echo "测试一下"           # 指定模型
./bin/llmhub route -m gpt-4o-mini -p "hi"          # 干跑：只告诉你会走哪个 provider，不发请求
./bin/llmhub stop
```

`llmhub` 等价于 `python3 daemon/llmhubd.py`，可以软链到 `PATH` 里：

```bash
ln -sf ~/.local/share/noctalia/plugins/ylxc-llm-hub/bin/llmhub ~/.local/bin/llmhub
```

### 方式 B：作为 Noctalia 插件

```bash
noctalia msg plugins enable ylxc/llm-hub     # 启用（本地插件目录会被自动发现）
noctalia msg panel-toggle ylxc/llm-hub:console   # 打开控制台面板
```

启用后：

- 状态栏出现图标（点左键打开控制台，点右键复制端点）；
- service 入口在后台轮询网关状态，并按设置里的 `autostart` 决定是否拉起守护进程；
- 设置里可以改端口、监听地址、访问密钥、配置文件路径、轮询间隔（状态栏 widget 还能单独配置显示内容）。

### 第一次跑通

默认配置里带了一个 **`mock` provider（`enabled: true`，无需任何 key）**，所以 `init` 之后立刻就能验证整条链路：

```bash
curl -s http://127.0.0.1:8787/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"model":"mock-echo","messages":[{"role":"user","content":"你好"}]}' | jq -r .choices[0].message.content
# 你好，我是内置模拟模型。
#
# 你刚才说：你好
```

想接真模型，改配置里的 api_key（支持环境变量占位）并把 `enabled` 打开：

```bash
export DEEPSEEK_API_KEY=sk-xxxx
./bin/llmhub set providers.1.enabled true      # 下标从 0 开始：1 == deepseek
./bin/llmhub reload
./bin/llmhub test deepseek                     # 连通性测试
./bin/llmhub ask -m deepseek-chat "你好"
```

---

## 本地接口

服务默认监听 `localhost:8787`，即**同时监听 `127.0.0.1:8787` 与 `[::1]:8787`**（仅本机可访问）——某些客户端（Node 18 的 fetch 等）会把 `localhost` 解析成 `::1`，只听 IPv4 时它们会直接报「连接被拒」。`server.host` 可写多个地址（逗号分隔）或 `0.0.0.0`/`*`。设了 `server.api_key` 之后，除 `/health` 外所有接口都要求 `Authorization: Bearer <key>` 或 `x-api-key: <key>`。

### 端点

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/v1/chat/completions`、`/chat/completions` | **OpenAI 兼容**聊天补全（流式/非流式） |
| POST | `/v1/completions`、`/completions` | OpenAI 旧版文本补全 |
| POST | `/v1/messages`、`/messages` | **Anthropic 兼容** Messages 接口 |
| POST | `/v1/messages/count_tokens` | Anthropic 计 token |
| POST | `/v1/responses` | **OpenAI Responses** 协议 |
| POST | `/v1/embeddings`、`/embeddings` | 转发到 provider 的 embeddings |
| POST | `/admin/ask` | 最简接口：`{"prompt":"…"}` → `{"text":"…","provider":…,"cost":…}` |
| POST | `/admin/test` | 测试某 provider（`{"provider":"deepseek"}`） |
| POST | `/admin/reload` | 重新读配置文件 |
| POST | `/admin/stats/reset`、`/admin/logs/clear` | 重置统计 / 清空日志 |
| POST | `/admin/providers/<id>/toggle` | 开关 provider（`{"enabled":true}`） |
| GET/POST | `/route` | **干跑**：返回这次请求会走哪个 provider、为什么（也可 `?model=&prompt=`） |
| GET | `/health` | 状态 + provider 概览（含 `missing_env`） |
| GET | `/v1/models`、`/models` | 模型目录（含别名） |
| GET | `/stats` | 用量统计快照 |
| GET | `/logs?limit=50` | 请求日志 |
| GET | `/config` | 当前配置（**密钥已打码**） |
| GET | `/` | 端点索引 |

### 请求级覆盖

不想改规则时，可以用请求头临时指定：

| 请求头 | 作用 |
| --- | --- |
| `X-LLMHub-Provider: deepseek` | 强制走某个 provider（仍会尝试其它候选做兜底） |
| `X-LLMHub-Scenario: 背景任务` | 指定场景（等价于把路径写成 `/s/背景任务/...`） |
| `X-LLMHub-Tags: batch,cheap` | 打标签，供规则的 `tags` 条件匹配 |

响应头会回带本次实际结果：`X-LLMHub-Provider`、`X-LLMHub-Model`、`X-LLMHub-Rule`、`X-LLMHub-Tactic`、`X-LLMHub-Latency-Ms`。

### 场景前缀

任意推理路径都可以加 `/s/<场景>/` 前缀，例如 `POST /s/背景任务/v1/chat/completions`，路由时优先使用该场景的配置（场景里可以声明 `tactic` / `providers` / `rules` / `model`）。

### 客户端接入示例

**OpenAI Python SDK**

```python
from openai import OpenAI

client = OpenAI(base_url="http://127.0.0.1:8787/v1", api_key="unused")
r = client.chat.completions.create(model="gpt-4o-mini", messages=[{"role": "user", "content": "你好"}])
print(r.choices[0].message.content)
```

**Anthropic SDK**

```python
import anthropic

client = anthropic.Anthropic(base_url="http://127.0.0.1:8787", api_key="unused")
m = client.messages.create(model="claude-sonnet-4-5", max_tokens=256,
                           messages=[{"role": "user", "content": "你好"}])
print(m.content[0].text)
```

**任何 CLI / 应用（环境变量式）**

```bash
export OPENAI_BASE_URL=http://127.0.0.1:8787/v1
export OPENAI_API_KEY=unused
```

**curl**

```bash
# 非流式
curl -s http://127.0.0.1:8787/v1/chat/completions -H 'Content-Type: application/json' \
  -d '{"model":"deepseek-chat","messages":[{"role":"user","content":"你好"}]}'

# 流式
curl -N http://127.0.0.1:8787/v1/chat/completions -H 'Content-Type: application/json' \
  -d '{"model":"deepseek-chat","stream":true,"messages":[{"role":"user","content":"写首诗"}]}'

# 最简
curl -s http://127.0.0.1:8787/admin/ask -H 'Content-Type: application/json' \
  -d '{"prompt":"一句话说明你是谁"}' | jq -r .text
```

---

## 配置

配置文件默认在 `~/.config/llm-hub/config.json`（可用 `LLMHUB_CONFIG` 或 `--config` 覆盖）。
数据目录默认 `~/.local/state/llm-hub`（pid、端口、日志、统计落在那里），Noctalia 插件模式下是插件的 `pluginDataDir()`。
保存后自动热重载（按 mtime 判断），也可以手动 `llmhub reload` 或 `POST /admin/reload`。

```jsonc
{
  "config_version": 1,
  "server": {
    "host": "localhost",        // localhost = 同时听 127.0.0.1 与 ::1；可写多个地址（逗号分隔）
    "port": 8787,                 // 被占用时 llmhubd 会自动挑下一个可用端口
    "api_key": "",                // 非空则要求 Bearer / x-api-key
    "request_timeout_ms": 600000, // 单次请求总预算（含重试与降级）
    "log_requests": true,
    "max_log_entries": 500
  },
  "guardrails": {
    "max_prompt_chars": 0,        // >0 时超长直接 400
    "block_patterns": [],         // 正则命中即拒绝（可写内部代号防泄漏）
    "mask_response": []           // 正则，命中的片段在回包前替换为 [masked]
  },
  "providers": [ /* 见下一节 */ ],
  "rules": [ /* 见路由规则一节 */ ],
  "default_route": { "providers": ["*"], "tactic": "priority", "fallback": true, "retries": 0 },
  "aliases": { "fast": "deepseek:deepseek-chat" },
  "scenarios": {
    "背景任务": { "tactic": "cheapest", "providers": ["mock", "*"] }
  }
}
```

字段值支持环境变量占位：`${VAR}`、`${VAR:-默认值}`、`env:VAR`。缺失的变量会记进该 provider 的 `missing_env`，在面板和 `/health` 里能看到（**不会**把 undefined 当字符串发出去）。

### 在控制台面板里管理服务商

`Provider` 页签是**两级视图**，所以整页不需要滚动：

1. **列表视图**：每个服务商一行（图标 + 名称 + `类型 · 启用状态 · N 个模型 · 密钥状态`），右侧是启停开关（点一下直接启用/停用）和一个铅笔按钮；列表下方是 `+ 添加服务商`。新增后会自动进入该服务商的编辑视图。
2. **编辑视图**（点铅笔进入）：左上角 `← 返回列表`，页面里只放这一个服务商的卡片，一屏放得下，里面的字段：

- **类型**：`openai` / `anthropic` / `gemini` / `ollama` / `mock` 一排按钮，点一下就换适配器（会同时重填该类型的默认 `base_url` / `path`）；
- **地址**：服务商 base URL，支持 `mock://local` 这类抽象地址与 `${VAR}` 占位；
- **密钥**：写成 password 输入框，**留空 = 不改动**（不会把已存的 key 清掉），也支持写 `${VAR}` 引用环境变量；
- **模型**：逗号分隔的模型名（可用 `*` 通配），**留空 = 该 provider 暂不参与路由**；
- 编辑视图底部同一行：请求数·平均延迟 · `连通性测试` · `保存` · `删除`（删除是两段式，点两次才真删）。
- **连通性测试**的结果直接回显上游答复，格式为 `模型 · 延迟 · tokens in+out` + 首行正文；失败时显示上游原话（如 `bad_request: 上游 HTTP 400: The supported API model names are …`）而不是笼统的「无法连接」——CLI 的 `--json` 结果只走 stdout，人读行一律走 stderr，面板才能稳定解析。

保存会写回 `config.json`（权限 0600），网关按 mtime 热重载，**运行中无需重启**。等价的手工方式：`llmhub provider-add …`、`llmhub provider-remove <id>`、`llmhub set providers.<id>.models '["a"]' --json`。

---

## 路由规则

一次请求的路由过程：

1. 解析入站协议 → 统一成内部请求（模型、消息、工具、图片、标签、prompt 估算长度…）；
2. 命中**第一条**满足 `when` 的规则（没命中就用 `default_route`；`aliases` 与 `X-LLMHub-Provider` 优先级更高，会钉住候选）；
3. 把规则里的 `providers` 规格展开成候选列表；
4. 按 `tactic` 排序候选；
5. 依次尝试，失败就降级到下一个（`fallback`），每个候选还能 `retries` 次。

### `when` 条件

| 字段 | 说明 |
| --- | --- |
| `model` | 模型名 glob 列表（`["*-mini", "claude-*"]`） |
| `provider` | 限定 provider id |
| `scenario` | 场景名 |
| `protocol` | `openai` / `anthropic` / `responses` |
| `has_tools` / `has_image` | 是否带工具 / 图片 |
| `tags` | 请求标签（`X-LLMHub-Tags`） |
| `min_prompt_tokens` / `max_prompt_tokens` | prompt 估算长度区间 |
| `prompt_regex` / `path_regex` | 对文本内容 / 请求路径做正则 |
| `stream` | 是否流式 |

规则里的 `providers` 规格语法：`"*"`（所有启用的 provider）、`"deepseek"`（该 provider 的默认模型）、`"deepseek:deepseek-chat"`（指定模型）、`"ollama:*"`。

### `tactic` 排序策略

| tactic | 含义 |
| --- | --- |
| `priority` | 按 provider 的 `priority` 数字升序（默认） |
| `cheapest` | 按估算成本（依 prompt 长度与 `price`） |
| `fastest` | 按延迟 EWMA 升序（没数据的排后面） |
| `least_used` | 按累计请求数升序（负载均衡） |
| `round_robin` | 轮转 |
| `random` | 随机 |
| `listed` | 严格保持规则里 `providers` 的书写顺序（适合「主 → 备」链路） |

规则可以写成嵌套 `route` 形式，也可以把 `providers`/`tactic`/`fallback`/`retries` 直接写在规则顶层（简写）：

```jsonc
{ "id": "cheap-background",
  "when": { "model": ["*-mini", "*-nano", "mock-*"], "has_tools": false },
  "route": { "providers": ["mock", "*"], "tactic": "cheapest", "fallback": true } }
```

### 在控制台面板里改规则

`路由规则` 页签和 Provider 页一样是**列表 ⇄ 编辑两级视图**，整页不需要滚动：

1. **列表视图**：一行一条规则，显示 `规则 id` + `tactic · 触发条件 · → 候选链`；右侧依次是 `↑`（上移，更优先）、`↓`、启停开关、铅笔。**规则顺序就是优先级**，第一条命中的规则生效。列表下面是 `+ 添加规则`，再往下是只读的默认路由、模型别名、场景端点。
2. **编辑视图**（点铅笔进入）：左上角 `← 返回列表`，右侧 `↑`/`↓` 也能直接调顺序，中间是一张规则卡片：

| 表单项 | 写到配置里的位置 | 说明 |
| --- | --- | --- |
| 描述 | `description` | 只是给人看的备注 |
| 模型匹配 | `when.model` | 逗号分隔、支持 glob，如 `*-mini, deepseek-*` |
| 服务商匹配 | `when.provider` | 限定 provider id |
| 标签匹配 | `when.tags` | 需要全部命中，对应 `X-LLMHub-Tags` |
| 场景匹配 | `when.scenario` | 对应 `/s/<场景>/…` |
| 最小 / 最大 prompt tokens | `when.min_prompt_tokens` / `when.max_prompt_tokens` | 按估算长度分档选模型（长上下文走贵模型、短问题走便宜模型） |
| 提示词正则 | `when.prompt_regex` | 对请求文本做正则 |
| 候选链 | `route.providers` | 逗号分隔的 provider id，支持 `*`；按顺序尝试 |
| 工具要求 | `when.has_tools` | `不限` / `必须带工具` / `必须不带工具` |
| 候选排序策略 | `route.tactic` | 七种 tactic 任选 |
| 失败回退 | `route.fallback` | 关掉就只试第一个候选 |

表单里的文本字段**留空 = 不限**（会把这个条件从配置里删掉）；底部 `保存` 只提交改动过的字段，`删除规则` 是两段式确认（点两次）。点 `+ 添加规则` 新建的规则**先保持停用**（无条件命中一切 + 候选链 `*` 会很霸道），填好条件和候选链后再打开卡片右上角的开关。

保存同样写回 `config.json` 并由网关按 mtime 热重载，**运行中即时生效**。等价的手工方式：

```bash
./bin/llmhub rule-add --id long-ctx --model "claude-*,*-128k" --providers "anthropic,openrouter" --tactic cheapest
./bin/llmhub set rules.long-ctx.route.tactic fastest                  # 改单个字段
./bin/llmhub set rules.long-ctx.when.model '["gpt-4o","o3-*"]' --json  # 多值字段
./bin/llmhub set rules.long-ctx.when.min_prompt_tokens --json null    # 清空某个条件（留空即不限）
./bin/llmhub rule-move long-ctx up                                   # 调优先级
./bin/llmhub rule-remove long-ctx                                    # 删规则
```

### 别名与场景

- `aliases`：把 `fast` 这种名字映射到 `"provider:model"`。请求 `model: "fast"` 时直接钉到该 provider（不再参与 tactic 重排）。
- `scenarios`：`/s/<场景>/...` 或 `X-LLMHub-Scenario` 进入的场景，可以覆盖 tactic/providers/rules/model。

干跑看结果：

```bash
./bin/llmhub route -m gpt-4o-mini -p "帮我写个快排"
# {"rule":"cheap-background","tactic":"cheapest","candidates":[{"provider":"mock",...}]}
```

---

## Provider 适配器

| `type` | 上游协议 | 默认路径 | 说明 |
| --- | --- | --- | --- |
| `openai` | OpenAI Chat Completions | `/v1/chat/completions` | 一切 OpenAI 兼容端点（DeepSeek、Moonshot、OpenRouter、vLLM、LM Studio、LocalAI…） |
| `anthropic` | Anthropic Messages | `/v1/messages` | 自动补 `x-api-key` 与 `anthropic-version` |
| `gemini` | Google Gemini | `/v1beta/models/{model}:generateContent` | 自动带 `?key=`，流式用 `alt=sse` |
| `ollama` | Ollama | `/api/chat` | 本地模型，默认不带鉴权 |
| `mock` | 无 | — | 内置模拟，用于自测（支持 `reply` / `echo` / `latency_ms` / `fail_times` / `fake_tool`） |

别名：`openai_compatible` / `custom` → `openai`，`claude` → `anthropic`，`google` → `gemini`。

provider 字段：

```jsonc
{
  "id": "deepseek",              // 必填，唯一
  "label": "DeepSeek 官方",
  "type": "openai",
  "enabled": true,
  "base_url": "https://api.deepseek.com",   // 别带 /v1，除非 path 里也写了
  "path": "/chat/completions",              // 可选，覆盖默认路径
  "api_key": "${DEEPSEEK_API_KEY}",
  "auth": { "type": "bearer" },             // bearer | header | query | none
  "headers": { "X-Foo": "bar" },
  "models": ["deepseek-chat", "deepseek-reasoner", "deepseek-*"],
  "model_map": { "gpt-4o": "deepseek-chat" },   // 客户端模型名 → 上游模型名
  "priority": 10,
  "price": { "in": 0.27, "out": 1.1 },      // 美元 / 百万 token，用于成本估算与 cheapest
  "rpm": 0,                                 // >0 时做每分钟请求数限流
  "timeout_ms": 120000,
  "tags": ["cheap"],
  "params": {
    "max_tokens_field": "max_tokens",
    "no_tools": false,            // 该上游不支持工具时置 true
    "stream_usage": true,         // OpenAI 流式是否带 stream_options.include_usage
    "anthropic_version": "2023-06-01",
    "api_version": "v1beta",
    "extra_body": { "top_k": 40 },
    "allow_insecure_tls": false
  },
  "mock": { "reply": "…", "echo": true, "latency_ms": 30, "fail_times": 0 }
}
```

`auth.type`：`bearer`（`Authorization: Bearer`）、`header`（自定义头，配 `auth.header` / `auth.prefix`）、`query`（拼到 URL，如 Gemini 的 `?key=`）、`none`。

---

## 协议翻译

网关内部统一成一套请求/响应模型，两端各自翻译，因此下面这些组合都成立：

| 客户端发的协议 | 上游 provider 类型 | 结果 |
| --- | --- | --- |
| OpenAI `/v1/chat/completions` | `anthropic` | Claude 收到 Anthropic Messages 格式，回包再翻回 OpenAI |
| Anthropic `/v1/messages` | `gemini` | Gemini 收到 `contents` + `systemInstruction`，回包翻成 Anthropic |
| Anthropic `/v1/messages` | `openai` | DeepSeek/vLLM 等直接接 Claude Code 这类客户端 |
| OpenAI `/v1/responses` | 任意 | Responses 协议进出 |
| 任意 | `ollama` | NDJSON 流被拼成标准 SSE 流 |

翻译覆盖：system 提示位置、工具 schema（`function.parameters` ↔ `input_schema` ↔ `functionDeclarations`）、`tool_choice`（`auto/any/none/tool` 三种写法互转，`required` → Gemini 的 `functionCallingConfig.mode=ANY`）、工具结果回传、多模态图片（含 data URL → base64）、流式增量事件、`finish_reason` / `stop_reason` / `finishReason` 映射、tokens 用量字段映射。

### 思考内容（reasoning / 思维链）也会透传

推理模型（DeepSeek-R1/V4、Claude thinking、Gemini thinking）把思维链放在单独字段里，网关会先归一成内部 `reasoning`，再按客户端协议输出，不会像普通反向代理那样丢掉：

| 上游的写法 | 客户端看到的 |
| --- | --- |
| OpenAI 风格 `reasoning_content`（DeepSeek 等） | OpenAI 协议 → `message.reasoning_content`；Anthropic 协议 → 开头的 `thinking` 块 |
| Anthropic 的 `thinking` 块 | 原样保留（或翻成 OpenAI 的 `reasoning_content`） |
| Gemini 的 `part.thought = true` | 同上 |

流式也一样：OpenAI 侧发 `delta.reasoning_content`，Anthropic 侧发 `content_block_start(type=thinking)` + `thinking_delta`，思考结束后再正常输出正文（`thinking` 块与 `text` 块自动分开，不会拼在一起）。

---

## CLI 参考

`./bin/llmhub <子命令>`（或 `python3 daemon/llmhubd.py …`）。全局参数：`--config`、`--data-dir`，可放在子命令前或后。

| 子命令 | 说明 |
| --- | --- |
| `serve` | 前台运行网关（`--host --port --quiet`） |
| `start` / `stop` / `restart` | 后台启停（pid + 端口文件 + 健康检查轮询） |
| `status` / `health` | 状态与 provider 概览 / 原始健康 JSON |
| `ask "问题"` | 一次性提问：`-m 模型` `--provider` `--scenario` `--system` `--max-tokens` `--stream` `--json` `-v` |
| `chat` | 交互式终端对话 |
| `route` | 规则干跑（`-m 模型` `-p "prompt"`） |
| `models` / `providers` | 列出模型（含别名）/ provider 与规则 |
| `stats` / `logs` / `clear-logs` / `reset-stats` | 统计、请求日志与清理（`--json`、`-n`、`--follow`、`--remote`） |
| `enable <id>` / `disable <id>` | 开关 provider（运行中即时生效并写回配置） |
| `test <id>` | 连通性测试（`-p` 自定义 prompt、`-m` 指定模型；不带 `-m` 时自动选该 provider 的第一个具体模型、`--json`） |
| `init` / `config` / `port` / `doctor` | 写配置模板 / 打印配置（密钥打码）/ 打印端口 / 环境自检 |
| `set <点分键> <值>` | 改配置，支持数组下标与 **id 定位**（`--json` 传 JSON 值，`--json null` = 删掉这个键）：`set providers.1.enabled true`、`set providers.deepseek.api_key sk-xxx`、`set providers.acme.models '["a","b"]' --json` |
| `provider-add` | 新增服务商：`provider-add --type anthropic --id acme --label 公司网关 --base-url https://… --key ${ACME_KEY} --model a --model b --priority 5`；默认 `type=openai`、`models=[]`（空 = 暂不参与路由） |
| `provider-remove <id\|下标>` | 删除服务商：`provider-remove acme` |
| `rule-add` | 新增路由规则：`rule-add --id long-ctx --model "claude-*" --providers "anthropic,openrouter" --tactic cheapest`；常用 flag：`--description` `--model` `--provider` `--scenario` `--tags` `--protocol` `--has-tools` `--has-image` `--min-prompt-tokens` `--max-prompt-tokens` `--prompt-regex` `--providers` `--tactic` `--retries` `--target-model` `--no-fallback` `--disabled` `--at <下标>` |
| `rule-move <id\|下标> up\|down` | 调整规则顺序（顺序即优先级） |
| `rule-remove <id\|下标>` | 删除规则：`rule-remove long-ctx` |

---

## Noctalia 插件

清单 `plugin.toml`（`plugin_api = 26`）声明了四个入口：

| 入口 | 文件 | 作用 |
| --- | --- | --- |
| `[[widget]] status`（`bar.luau`） | 状态栏 | 默认只有图标（运行中为强调色）+ 悬停详情 tooltip，不带任何计数；可选在图标旁显示平均延迟。左键开控制台，右键复制端点 |
| `[[panel]] console`（`panel.luau`） | 控制台面板（660×800） | 概览 / Provider / 路由规则 / 问答 / 日志 五个页签：Provider 页签是**列表 ⇄ 编辑两级视图**（列表里一行一个服务商，直接开关、点铅笔进编辑视图改类型/地址/密钥/模型，底部「+ 添加服务商」，整页无需滚动）；路由规则页签同样是**列表 ⇄ 编辑两级视图**（开关启停、`↑`/`↓` 调优先级、铅笔编辑条件与候选链、新增/删除规则）；另有启停、重启、重载配置、清日志、重置统计、提问、干跑路由、看请求日志 |
| `[[shortcut]] toggle`（`shortcut.luau`） | 快捷键 | 一键启停网关（右键打开面板） |
| `[[service]] hub`（`service.luau`） | 后台服务 | 轮询网关状态并发布到共享状态；处理 IPC 事件；按设置决定退出时是否停网关 |

设置项（设置 → 插件 → LLM Hub）：`autostart`、`stop_on_exit`、`port`、`host`、`api_key`、`config_path`、`poll_interval_ms`；widget 实例另有 `glyph`、`show_text`、`show_latency`（栏上不显示请求数等计数，完整用量在悬停详情与控制台面板里）。

IPC 事件：

```bash
noctalia msg plugin ylxc/llm-hub:hub all start      # 也支持 stop / restart / refresh / status
noctalia msg plugin ylxc/llm-hub:hub all ask "你好"  # 提问，结果落到共享状态
noctalia msg plugin ylxc/llm-hub:status - toggle    # widget 入口（需要指定 bar 目标）
noctalia msg plugin ylxc/llm-hub:console all test deepseek      # 面板里跑一次连通性测试（target 必须写 all）
noctalia msg panel-toggle ylxc/llm-hub:console
noctalia msg panel-open ylxc/llm-hub:console providers        # 直接开在 Provider 页
noctalia msg panel-open ylxc/llm-hub:console test:deepseek    # 开在 Provider 页并顺手测一下
noctalia msg panel-open ylxc/llm-hub:console rule:long-ctx    # 开在规则编辑视图
noctalia msg panel-open ylxc/llm-hub:console logs             # 直接开在日志页
```

`panel-open` 的第二个参数（context）可以接 `overview` / `providers` / `providers:<id>` / `rules` / `rule:<id>` / `ask` / `logs` / `test[:<id>]`，栏按钮或脚本可据此直接定位到某一页；面板的 `test` 事件同样支持带一个 provider id，跑完把结果直接显示在 Provider 页。

插件把守护进程的端口/pid/日志放在 `pluginDataDir()`（跨插件更新存活），配置文件默认仍是 `~/.config/llm-hub/config.json`。

---

## 目录结构

```
ylxc-llm-hub/
├── plugin.toml              # Noctalia 插件清单（4 个入口 + 设置项）
├── bar.luau                 # 状态栏 widget
├── panel.luau               # 控制台面板（5 页签）
├── shortcut.luau            # 启停快捷键
├── service.luau             # 后台服务（轮询 / IPC / 生命周期）
├── lib/hub.luau             # 共享模块：设置、路径、CLI 调用、快照与格式化
├── translations/            # zh-Hans.json / en.json
├── bin/llmhub               # 免安装 CLI 入口
├── daemon/
│   ├── llmhubd.py           # CLI + 进程管理 + 本地调用客户端
│   ├── llmhub_server.py     # asyncio HTTP 网关（路由分发、SSE、鉴权、日志）
│   ├── llmhub_core.py       # 配置、路由引擎、统计、执行器、护栏
│   ├── llmhub_adapters.py   # 上游适配器（openai/anthropic/gemini/ollama/mock）
│   └── llmhub_canon.py      # 协议规范化与三种协议的序列化
└── tests/
    ├── smoke_test.py        # 网关端到端冒烟（88 项，无需联网）
    └── translate_test.py    # 协议翻译矩阵（41 项，自带假上游）
```

---

## 测试

两套测试都**不需要联网、不需要任何 API key**（用 `type=mock` 的 provider 与本地假上游）：

```bash
python3 tests/smoke_test.py       # 通过 88 项，失败 0 项
python3 tests/translate_test.py   # 通过 41 项，失败 0 项
```

`smoke_test.py` 覆盖健康检查/鉴权、路由与 tactic、别名、场景前缀、流式响应头、统计分组、`/config` 打码、热重载、错误处理、优雅退出，以及**路由规则的 CLI 增删改**（面板「添加规则 / 保存 / 开关 / 上移下移 / 删除」走的都是这些通路）。
`translate_test.py` 起一个假上游服务器，逐个断言 OpenAI↔Anthropic↔Gemini↔Ollama 的请求翻译与响应翻译（含工具调用、图片、SSE、用量映射、思考内容透传）。

Luau 侧：

```bash
luau-analyze --mode=nonstrict *.luau lib/*.luau    # 静态检查
noctalia plugins lint .                            # 清单与设置项校验（0 error 0 warning）
```

---

## 安全

- 网关**默认只监听 `127.0.0.1`**，不对外网暴露；想给别的机器用请自己加反代并设置 `server.api_key`。
- `GET /config`、`llmhub config` 输出的密钥一律打码（`***`），URL 查询串里的 `key`/`token` 也会打码。
- `api_key` 支持 `${ENV_VAR}` 占位，推荐把密钥放在环境变量而不是配置明文里；缺失的变量会显示为 `missing_env` 提示，不会静默失败。
- `guardrails.block_patterns` 可以在网关侧直接拦住包含特定内容的 prompt，`mask_response` 可以在回包前替换敏感片段。
- 请求日志只记录元信息（协议、模型、provider、规则、耗时、tokens、UA），不落 prompt 与回复正文。

## 常见问题

**端口被占用？** `llmhubd` 会自动往后找可用端口（最多 20 次），用 `llmhub port` 查实际端口；插件模式下端口是从 data 目录的 `llmhubd.port` 回读的。

**Noctalia 显示离线？** `settings.shell.offline_mode` 只会影响插件的 HTTP 能力，本插件的 UI 一律走 CLI（`runAsync`）通信，因此离线也能用。

**provider 显示「缺少环境变量」？** 说明配置里写了 `${X_API_KEY}` 但当前进程环境里没有；在面板的 Provider 页签能看到具体缺哪个。

**流式没输出？** 确认客户端不会缓冲（curl 加 `-N`），网关对 SSE 用的是 chunked 传输，上游首个事件到达后才写响应头。

**想换配置文件位置？** 启动前设 `LLMHUB_CONFIG`，或在插件设置里填 `config_path`，或加 `--config`。

## License

MIT
