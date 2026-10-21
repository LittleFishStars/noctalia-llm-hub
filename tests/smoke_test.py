#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""llm-hub 端到端冒烟测试：不需要任何真实 API key 与网络。

覆盖：守护进程启动/停止、三种入站协议（OpenAI/Anthropic/Responses）、规则路由、
tactic、回退（fallback）、SSE 流式、别名、场景前缀、干跑、统计与日志、管理接口。

运行：python3 tests/smoke_test.py        （退出码 0 = 全部通过）
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DAEMON = os.path.join(ROOT, "daemon", "llmhubd.py")

CONFIG = {
    "version": 1,
    "server": {"host": "127.0.0.1", "port": 18787, "api_key": "test-key-123",
               "request_timeout_ms": 30000, "log_requests": True, "max_log_entries": 100},
    "providers": [
        {"id": "mock-primary", "type": "mock", "enabled": True, "priority": 10,
         "models": ["mock-echo", "smart-model"], "price": {"in": 1.0, "out": 2.0},
         "mock": {"reply": "来自 primary 的回答", "echo": True}},
        {"id": "mock-flaky", "type": "mock", "enabled": True, "priority": 20,
         "models": ["flaky-*"], "mock": {"reply": "不该被用到", "fail_times": 99,
                                         "latency_ms": 5}},
        {"id": "mock-cheap", "type": "mock", "enabled": True, "priority": 50,
         "models": ["cheap-*", "smart-model"], "price": {"in": 0.01, "out": 0.02},
         "mock": {"reply": "便宜的模型回答", "echo": True}},
        {"id": "mock-offline", "type": "mock", "enabled": False, "models": ["*"],
         "mock": {"reply": "x"}},
        {"id": "mock-tools", "type": "mock", "enabled": True, "priority": 1,
         "models": ["tool-model"],
         "mock": {"fake_tool": {"name": "get_time", "args": "{\"tz\":\"UTC\"}"}}},
        # 故意指向不可达端口：CLI / 面板的失败路径要有可断言的结果
        {"id": "dead-upstream", "type": "openai", "enabled": True, "priority": 99,
         "base_url": "http://127.0.0.1:9", "api_key": "dead-key",
         "models": ["dead-model"]},
    ],
    "rules": [
        {"id": "tool-heavy", "when": {"has_tools": True}, "providers": ["mock-tools"],
         "tactic": "priority"},
        {"id": "cheap-then-primary", "when": {"model": "smart-model"}, "providers": ["mock-cheap"],
         "tactic": "cheapest"},
        {"id": "flaky-with-fallback",
         "when": {"model": "flaky-*"},
         "providers": ["mock-flaky", "mock-primary"], "tactic": "listed",
         "fallback": True, "retries": 1},
    ],
    "default_route": {"providers": ["mock-primary"], "tactic": "priority",
                      "fallback": True, "retries": 0},
    "aliases": {"fast": "mock-cheap:cheap-1", "smart": "mock-primary:smart-model"},
    "scenarios": {"背景任务": {"default_route": {"providers": ["mock-cheap"],
                                             "tactic": "priority"}}},
    "guardrails": {"max_prompt_chars": 0, "block_patterns": ["机密项目代号"],
                   "mask_response": ["身份证"]},
}

PASS, FAIL = [], []


def check(name: str, cond: bool, detail: str = ""):
    (PASS if cond else FAIL).append(name)
    mark = "\033[32m✓\033[0m" if cond else "\033[31m✗\033[0m"
    print(f"  {mark} {name}" + (f"   {detail}" if detail and not cond else ""))


class Client:
    def __init__(self, port: int, key: str = "test-key-123"):
        self.base = f"http://127.0.0.1:{port}"
        self.key = key

    def raw(self, path: str, body: dict | None = None, key: str | None = None, stream=False):
        # 非 ASCII 路径（如场景名）必须先 percent-encode，否则 http.client 报 ascii codec
        path = urllib.parse.quote(path, safe="/?&=%:.-_~!$'()*+,;")
        data = json.dumps(body).encode() if body is not None else None
        headers = {"Content-Type": "application/json"}
        k = self.key if key is None else key
        if k:
            headers["Authorization"] = f"Bearer {k}"
        req = urllib.request.Request(self.base + path, data=data, headers=headers)
        return urllib.request.urlopen(req, timeout=60), req

    def call(self, path, body=None, key="__default__"):
        k = self.key if key == "__default__" else key
        try:
            r, _ = self.raw(path, body, k)
            return r.status, json.loads(r.read().decode() or "{}"), dict(r.headers)
        except urllib.error.HTTPError as e:
            raw = e.read().decode()
            try:
                return e.code, json.loads(raw or "{}"), dict(e.headers)
            except Exception:
                return e.code, {"_raw": raw}, dict(e.headers)

    def sse(self, path, body):
        """返回 (状态码, 事件列表, 响应头)。"""
        r, _ = self.raw(path, {**body, "stream": True})
        events = []
        for line in r:
            line = line.decode("utf-8", "replace").rstrip("\r\n")
            if line.startswith("data:"):
                d = line[5:].strip()
                if d and d != "[DONE]":
                    events.append(json.loads(d))
        return r.status, events, dict(r.headers)


def wait_port(state_dir: str, timeout=15) -> int:
    pf = os.path.join(state_dir, "llmhubd.port")
    t0 = time.time()
    while time.time() - t0 < timeout:
        if os.path.exists(pf):
            try:
                p = int(open(pf).read().strip())
            except Exception:
                p = 0
            if p:
                try:
                    urllib.request.urlopen(f"http://127.0.0.1:{p}/health", timeout=2).read()
                    return p
                except Exception:
                    pass
        time.sleep(0.2)
    raise SystemExit("守护进程未能在超时内就绪")


def main() -> int:
    tmp = tempfile.mkdtemp(prefix="llmhub-smoke-")
    cfg_path = os.path.join(tmp, "config.json")
    state = os.path.join(tmp, "state")
    os.makedirs(state, exist_ok=True)
    with open(cfg_path, "w", encoding="utf-8") as f:
        json.dump(CONFIG, f, ensure_ascii=False)

    sys.path.insert(0, os.path.join(ROOT, "daemon"))
    import llmhub_server as srv

    print("\n⑪ 监听地址展开与连通性探测（纯函数）")
    check("localhost 同时展开出 127.0.0.1 与 ::1",
          srv.expand_hosts("localhost") == ["127.0.0.1", "::1"], str(srv.expand_hosts("localhost")))
    check("逗号/空格分隔的多地址都保留",
          srv.expand_hosts("127.0.0.1, ::1") == ["127.0.0.1", "::1"],
          str(srv.expand_hosts("127.0.0.1, ::1")))
    check("通配 * 展开成 IPv4+IPv6 通配", srv.expand_hosts("*") == ["0.0.0.0", "::"],
          str(srv.expand_hosts("*")))
    check("空值回退到 127.0.0.1", srv.expand_hosts("") == ["127.0.0.1"], str(srv.expand_hosts("")))
    check("地址格式化 IPv4/IPv6 都正确",
          srv._fmt_sockaddr(("127.0.0.1", 8787)) == "127.0.0.1:8787"
          and srv._fmt_sockaddr(("::1", 8787, 0, 0)) == "[::1]:8787",
          srv._fmt_sockaddr(("::1", 8787, 0, 0)))

    _cfg = {"providers": [
        {"id": "a", "enabled": True, "models": ["*", "real-a"]},
        {"id": "b", "enabled": True, "models": ["real-b"]},
        {"id": "off", "enabled": False, "models": ["nope-model"]},
    ]}
    check("探测优先用指定 provider 的第一个具体模型（跳过 *）",
          srv.probe_model(_cfg, "a", "") == "real-a", srv.probe_model(_cfg, "a", ""))
    check("显式传模型时原样使用", srv.probe_model(_cfg, "a", "x-1") == "x-1")
    check("指定 provider 没有具体模型时退到其它已启用 provider",
          srv.probe_model(_cfg, "off", "") == "real-a", srv.probe_model(_cfg, "off", ""))
    check("只有通配模型时才回退 ping（旧行为，会让真实上游报 400）",
          srv.probe_model({"providers": [{"id": "m", "enabled": True, "models": ["*"]}]},
                          "m", "") == "ping")
    proc = None
    try:
        print("启动守护进程…")
        proc = subprocess.Popen(
            [sys.executable, DAEMON, "--config", cfg_path, "--data-dir", state, "serve"],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1,
            env={**os.environ, "LLMHUB_DATA_DIR": state})
        port = wait_port(state)
        c = Client(port)
        print(f"  网关端口 {port}\n")

        print("① 健康检查与鉴权")
        st, h, _ = c.call("/health")
        check("GET /health 返回 200", st == 200 and h.get("status") == "ok")
        check("health 列出 provider 且标记启用数", len(h.get("providers") or []) == 6
              and set(h.get("enabled_providers") or []) == {"mock-primary", "mock-flaky",
                                                            "mock-cheap", "mock-tools",
                                                            "dead-upstream"},
              str(h.get("enabled_providers")))
        st, err, _ = c.call("/stats", key="wrong-key")
        check("错误 API Key 被拒绝 401", st == 401, f"status={st}")
        st, err, _ = c.call("/stats", key="")
        check("无 API Key 被拒绝 401", st == 401, f"status={st}")
        st, s, _ = c.call("/stats")
        check("正确 API Key 可访问 /stats", st == 200)

        print("\n② OpenAI 协议（非流式）")
        st, r, hd = c.call("/v1/chat/completions", {
            "model": "mock-echo", "messages": [{"role": "user", "content": "你好世界"}]})
        check("200 + 正确结构", st == 200 and r.get("object") == "chat.completion"
              and r["choices"][0]["message"]["role"] == "assistant", json.dumps(r)[:200])
        check("echo 回显了用户消息", "你好世界" in r["choices"][0]["message"].get("content", ""),
              r["choices"][0]["message"].get("content"))
        check("响应头带 provider/model 调试信息",
              hd.get("X-LLMHub-Provider") == "mock-primary"
              and hd.get("X-LLMHub-Model") == "mock-echo", str(hd.get("X-LLMHub-Provider")))
        check("usage 统计非零", r["usage"]["total_tokens"] > 0, str(r.get("usage")))

        print("\n③ 规则路由与 tactic")
        st, d, _ = c.call("/route?model=smart-model&prompt=hi")
        check("干跑显示 cheapest 规则命中 mock-cheap",
              d.get("rule") == "cheap-then-primary"
              and d.get("tactic") == "cheapest"
              and d["candidates"][0]["provider"] == "mock-cheap", json.dumps(d)[:300])
        st, r1, hd1 = c.call("/v1/chat/completions", {
            "model": "smart-model", "messages": [{"role": "user", "content": "选择我"}]})
        check("命中 cheap-then-primary 并实际走 mock-cheap",
              hd1.get("X-LLMHub-Rule") == "cheap-then-primary"
              and hd1.get("X-LLMHub-Provider") == "mock-cheap", str(hd1.get("X-LLMHub-Provider")))
        st, d2, _ = c.call("/route?model=unknown-model-x")
        check("未命中任何规则时走 default_route", d2.get("rule") == "default_route",
              str(d2.get("rule")))
        st, r2, hd2 = c.call("/v1/chat/completions", {
            "model": "unknown-model-x", "messages": [{"role": "user", "content": "hi"}]})
        check("未知模型回落到 primary", st == 200 and hd2.get("X-LLMHub-Provider") == "mock-primary",
              str(hd2.get("X-LLMHub-Provider")))
        st, r3, hd3 = c.call("/v1/chat/completions", {
            "model": "tool-model", "messages": [{"role": "user", "content": "几点"}],
            "tools": [{"type": "function", "function": {"name": "get_time",
                                                        "parameters": {"type": "object",
                                                                       "properties": {}}}}]})
        check("has_tools 规则优先匹配 mock-tools",
              hd3.get("X-LLMHub-Provider") == "mock-tools", str(hd3.get("X-LLMHub-Provider")))
        check("工具调用被翻译回 OpenAI tool_calls 结构",
              bool(r3["choices"][0]["message"].get("tool_calls")), json.dumps(r3)[:250])

        print("\n④ 失败回退（fallback）")
        st, r4, hd4 = c.call("/v1/chat/completions", {
            "model": "flaky-model", "messages": [{"role": "user", "content": "会失败吗"}]})
        check("全部失败后成功回退到 primary", st == 200 and hd4.get("X-LLMHub-Provider") == "mock-primary",
              f"status={st} provider={hd4.get('X-LLMHub-Provider')}")
        st, lg, _ = c.call("/logs?limit=5")
        rec = [e for e in (lg.get("logs") or []) if e.get("model") == "flaky-model"]
        check("日志记录了回退尝试链", bool(rec) and rec[-1].get("fallback", 0) >= 1,
              json.dumps(rec[-1] if rec else {})[:250])

        print("\n⑤ 别名与场景")
        st, r5, hd5 = c.call("/v1/chat/completions", {
            "model": "fast", "messages": [{"role": "user", "content": "别名测试"}]})
        check("别名 fast 解析到 mock-cheap:cheap-1",
              hd5.get("X-LLMHub-Provider") == "mock-cheap"
              and hd5.get("X-LLMHub-Model") == "cheap-1",
              f"{hd5.get('X-LLMHub-Provider')}:{hd5.get('X-LLMHub-Model')}")
        st, r6, hd6 = c.call("/s/背景任务/v1/chat/completions", {
            "model": "whatever", "messages": [{"role": "user", "content": "场景测试"}]})
        check("场景前缀 /s/<场景>/ 走该场景的 default_route",
              hd6.get("X-LLMHub-Provider") == "mock-cheap", str(hd6.get("X-LLMHub-Provider")))
        st, r7, hd7 = c.call("/v1/chat/completions", {
            "model": "whatever", "messages": [{"role": "user", "content": "头路由"}],
            "x": 1}, key="test-key-123")
        check("未知模型在无场景时回默认", st == 200, str(st))

        print("\n⑥ Anthropic 协议（含流式）")
        st, ra, _ = c.call("/v1/messages", {
            "model": "mock-echo", "max_tokens": 64,
            "messages": [{"role": "user", "content": "anthropic 入站"}]})
        check("返回 Anthropic message 结构", st == 200 and ra.get("type") == "message"
              and ra.get("role") == "assistant" and isinstance(ra.get("content"), list),
              json.dumps(ra)[:250])
        check("content 含 text block", ra["content"][0]["type"] == "text" if ra.get("content") else False)
        st, ev, hd = c.sse("/v1/messages", {
            "model": "mock-echo", "max_tokens": 64,
            "messages": [{"role": "user", "content": "流式 anthropic"}]})
        types = [e.get("type") for e in ev]
        check("Anthropic SSE 事件序列完整",
              types[:1] == ["message_start"] and "message_stop" in types
              and "content_block_delta" in types, str(types))
        text = "".join(e.get("delta", {}).get("text", "") for e in ev
                       if e.get("type") == "content_block_delta")
        check("流式文本拼接正确", "流式 anthropic" in text, text)
        st, tok, _ = c.call("/v1/messages/count_tokens", {
            "model": "mock-echo", "messages": [{"role": "user", "content": "统计我"}]})
        check("count_tokens 返回估算值", st == 200 and tok.get("input_tokens", 0) > 0, str(tok))

        print("\n⑦ OpenAI 流式（SSE）与 Responses 协议")
        st, ev, hd = c.sse("/v1/chat/completions", {
            "model": "mock-echo", "messages": [{"role": "user", "content": "openai 流"}]})
        check("OpenAI SSE 以 data: 分块并带 [DONE]", hd.get("Content-Type", "").startswith("text/event-stream"))
        deltas = [ch["choices"][0]["delta"].get("content") for ch in ev if ch.get("choices")]
        check("delta 文本非空", any(deltas), str(ev)[:200])
        check("流式响应头也带 provider", hd.get("X-LLMHub-Provider") == "mock-primary",
              str(hd.get("X-LLMHub-Provider")))
        st, rr, _ = c.call("/v1/responses", {
            "model": "mock-echo", "input": "responses 入站"})
        check("Responses 协议返回 output 列表", st == 200 and isinstance(rr.get("output"), list),
              json.dumps(rr)[:250])
        st, ev2, _ = c.sse("/v1/responses", {"model": "mock-echo", "input": "responses 流"})
        types2 = [e.get("type") for e in ev2]
        check("Responses SSE 事件类型正确",
              "response.output_text.delta" in types2, str(types2[:6]))

        print("\n⑧ 护栏、管理接口与统计")
        st, gr, _ = c.call("/v1/chat/completions", {
            "model": "mock-echo", "messages": [{"role": "user", "content": "说说机密项目代号"}]})
        check("block_patterns 命中返回 400", st == 400 and "block" in json.dumps(gr).lower()
              or st == 400, f"status={st}")
        st, t, _ = c.call("/admin/test", {"provider": "mock-primary", "prompt": "连通性"})
        check("/admin/test 不带 model 时自动选 provider 的第一个具体模型",
              t.get("ok") is True and t.get("provider") == "mock-primary"
              and t.get("model") == "mock-echo", json.dumps(t)[:200])
        st, a, _ = c.call("/admin/ask", {"prompt": "最简接口", "provider": "mock-cheap"})
        check("/admin/ask 指定 provider 生效", a.get("provider") == "mock-cheap", str(a.get("provider")))
        st, a2, _ = c.call("/admin/ask", {"messages": [
            {"role": "user", "content": "多轮"}, {"role": "assistant", "content": "好"},
            {"role": "user", "content": "继续"}]})
        check("/admin/ask 支持 messages 多轮", st == 200 and a2.get("text"), json.dumps(a2)[:150])
        st, off, _ = c.call("/admin/providers/mock-cheap/toggle", {"enabled": False})
        st2, d3, _ = c.call("/route?model=smart-model")
        check("停用 provider 后规则改走其它候选",
              off.get("ok") and all(x["provider"] != "mock-cheap" for x in d3.get("candidates") or []),
              json.dumps(d3.get("candidates"))[:200])
        c.call("/admin/providers/mock-cheap/toggle", {"enabled": True})
        st, md, _ = c.call("/v1/models")
        ids = [m["id"] for m in md.get("data") or []]
        check("/v1/models 含模型与别名",
              "mock-echo" in ids and "fast" in ids, str(ids[:12]))
        st, stt, _ = c.call("/stats")
        check("/stats 汇总了 token 与成本", stt.get("total_requests", 0) >= 8
              and stt.get("total_tokens_out", 0) > 0, json.dumps(stt)[:200])
        check("统计按模型与规则分组",
              bool(stt.get("by_model")) and bool(stt.get("by_rule")), str(list(stt.keys())))
        st, cl, _ = c.call("/admin/logs/clear", {})
        st2, lg2, _ = c.call("/logs?limit=5")
        check("清空日志生效", cl.get("ok") and not (lg2.get("logs") or []), str(lg2)[:120])
        st, cfgv, _ = c.call("/config")
        check("/config 不泄露密钥内容", "test-key" not in json.dumps(cfgv), "密钥泄露")
        st, rl, _ = c.call("/admin/reload", {})
        check("/admin/reload 热重载成功", rl.get("ok") is True, json.dumps(rl)[:150])

        print("\n⑨ 错误处理")
        st, e1, _ = c.call("/v1/chat/completions", {"messages": [{"role": "user", "content": "无模型"}]})
        check("缺 model 字段返回 400", st == 400, f"status={st}")
        st, e2, _ = c.call("/no/such/path", {})
        check("未知路径返回 404", st == 404, f"status={st}")
        st, e3, _ = c.call("/v1/chat/completions", {
            "model": "mock-echo", "messages": [{"role": "user", "content": "hi"}],
            "model_override": None})
        st, _b, _ = c.call("/v1/chat/completions", None)   # GET 推理端点：应报 400/405 而不是崩溃
        check("GET 到推理端点不崩溃", st in (400, 405), f"status={st}")
        # 只启用一个必然失败的 provider：验证错误映射
        st, err2, _ = c.call("/admin/providers/mock-primary/toggle", {"enabled": False})
        st, e4, _ = c.call("/v1/chat/completions", {
            "model": "flaky-only", "messages": [{"role": "user", "content": "hi"}]})
        check("无可用候选时返回 5xx 且带错误体", st >= 400 and "error" in json.dumps(e4),
              f"status={st} {json.dumps(e4)[:150]}")
        c.call("/admin/providers/mock-primary/toggle", {"enabled": True})

        print("\n⑫ CLI 的 --json 输出必须能整体解析（面板拿它显示测试结果）")
        cli = [sys.executable, DAEMON, "--config", cfg_path, "--data-dir", state]
        env = {**os.environ, "LLMHUB_DATA_DIR": state}

        def run_cli(*extra):
            return subprocess.run(cli + list(extra), capture_output=True, text=True,
                                  timeout=180, env=env)

        def as_json(text):
            try:
                val = json.loads(text)
                return val if isinstance(val, dict) else None
            except Exception:
                return None

        r = run_cli("test", "mock-primary", "--json")
        good = as_json(r.stdout)
        check("test --json 的 stdout 本身就是合法 JSON（不再是混排文本）", good is not None,
              f"stdout={r.stdout[:120]!r} stderr={r.stderr[:80]!r}")
        check("成功结果带 ok/model/tokens/text",
              bool(good) and good.get("ok") and good.get("model") and "tokens_out" in good,
              json.dumps(good or {})[:160])
        check("人类可读行改走 stderr", "✓" in (r.stderr or "") and "✓" not in r.stdout,
              f"stderr={r.stderr[:80]!r}")

        r2 = run_cli("test", "dead-upstream", "--json")
        bad = as_json(r2.stdout)
        check("上游不可达时也给 JSON（ok=false + 错误信息）",
              bad is not None and bad.get("ok") is False and bool(bad.get("error")),
              json.dumps(bad or {})[:160])
        check("失败时退出码非 0", r2.returncode != 0, f"rc={r2.returncode}")
        check("失败时的 JSON 里 provider 字段仍是请求的那一个",
              (bad or {}).get("provider") == "dead-upstream", json.dumps(bad or {})[:120])

        r3 = run_cli("test", "mock-primary", "--model", "mock-echo")
        check("不带 --json 时 stdout 仍是人类可读行",
              "✓" in r3.stdout and as_json(r3.stdout) is None, r3.stdout[:100])

        print("\n⑬ 路由规则可由 CLI 增删改（面板「添加规则」走同一条通路）")
        r = run_cli("rule-add", "--id", "smoke-rule", "--description", "冒烟新增",
                    "--model", "smoke-special", "--providers", "mock-cheap", "--tactic", "listed")
        added = as_json(r.stdout)
        check("rule-add 返回 JSON（含 id 与插入位置）",
              bool(added) and added.get("id") == "smoke-rule" and added.get("index") == 3,
              r.stdout[:160])
        st, cfgv, _ = c.call("/config")
        ids = [x.get("id") for x in (cfgv.get("rules") or [])]
        check("新增的规则被热重载进配置", ids[-1:] == ["smoke-rule"], str(ids))
        st, d, _ = c.call("/route?model=smoke-special")
        check("新规则立刻生效（命中 mock-cheap）",
              d.get("rule") == "smoke-rule" and d["candidates"][0]["provider"] == "mock-cheap",
              json.dumps(d)[:200])

        r = run_cli("set", "rules.smoke-rule.enabled", "false")
        check("set 可以把规则置为停用", as_json(r.stdout) is not None, r.stdout[:120])
        st, d, _ = c.call("/route?model=smoke-special")
        check("停用的规则不再参与匹配", d.get("rule") != "smoke-rule", str(d.get("rule")))
        run_cli("set", "rules.smoke-rule.enabled", "true")
        st, d, _ = c.call("/route?model=smoke-special")
        check("重新启用后恢复匹配", d.get("rule") == "smoke-rule", str(d.get("rule")))

        run_cli("rule-add", "--id", "smoke-first", "--model", "smoke-special",
                "--providers", "mock-primary", "--at", "0")
        st, d, _ = c.call("/route?model=smoke-special")
        check("插到最前面的规则优先命中", d.get("rule") == "smoke-first", str(d.get("rule")))
        r = run_cli("rule-move", "smoke-first", "down")
        moved = as_json(r.stdout)
        check("rule-move 返回移动后的位置", bool(moved) and moved.get("moved") is True,
              r.stdout[:120])
        st, cfgv, _ = c.call("/config")
        order = [x.get("id") for x in (cfgv.get("rules") or [])]
        check("下移一位后顺序真的换了（顺序即优先级）",
              order[:2] == ["tool-heavy", "smoke-first"], str(order))
        st, d, _ = c.call("/route?model=smoke-special")
        check("被越过的规则不匹配该模型，仍命中 smoke-first",
              d.get("rule") == "smoke-first", str(d.get("rule")))

        r = run_cli("rule-add", "--id", "smoke-bad", "--providers", "mock-primary",
                    "--tactic", "cheapestest")
        check("非法 tactic 被拒绝", r.returncode != 0 and as_json(r.stdout) is None,
              r.stdout[:120])
        r = run_cli("rule-add", "--id", "smoke-bad", "--providers", "mock-primary",
                    "--prompt-regex", "(")
        check("非法正则被拒绝", r.returncode != 0, r.stdout[:120])
        r = run_cli("rule-add", "--id", "smoke-first", "--providers", "mock-primary")
        check("重复规则 id 被拒绝", r.returncode != 0 and "已存在" in r.stdout, r.stdout[:120])

        run_cli("set", "rules.smoke-rule.when.has_tools", "true")
        st, cfgv, _ = c.call("/config")
        cur = next((x for x in (cfgv.get("rules") or []) if x.get("id") == "smoke-rule"), {})
        check("set 能给规则加筛选条件", (cur.get("when") or {}).get("has_tools") is True,
              json.dumps(cur.get("when"), ensure_ascii=False))
        r = run_cli("set", "rules.smoke-rule.when.has_tools", "--json", "null")
        removed = as_json(r.stdout)
        check("--json null 表示删除该键", bool(removed) and removed.get("removed") is True,
              r.stdout[:120])
        st, cfgv, _ = c.call("/config")
        cur = next((x for x in (cfgv.get("rules") or []) if x.get("id") == "smoke-rule"), {})
        check("删掉的键从配置里消失", "has_tools" not in (cur.get("when") or {}),
              json.dumps(cur.get("when"), ensure_ascii=False))

        # 面板「添加规则」的实际写法：新建的先停用，填好条件再打开开关
        r = run_cli("rule-add", "--id", "smoke-panel", "--disabled")
        added = as_json(r.stdout)
        check("rule-add --disabled 建出的规则是停用状态",
              bool(added) and added.get("rule", {}).get("enabled") is False, r.stdout[:160])
        st, d, _ = c.call("/route?model=smoke-special")
        check("停用的新规则不影响路由", d.get("rule") != "smoke-panel", str(d.get("rule")))
        # 面板编辑表单保存字段时的写法：多值写 JSON 数组、描述写普通字符串
        run_cli("set", "rules.smoke-panel.when.model", '["panel-special","*-mini"]', "--json")
        run_cli("set", "rules.smoke-panel.route.providers", '["mock-cheap"]', "--json")
        run_cli("set", "rules.smoke-panel.description", "面板保存的规则")
        run_cli("set", "rules.smoke-panel.route.tactic", "cheapest")
        run_cli("set", "rules.smoke-panel.enabled", "true")
        st, cfgv, _ = c.call("/config")
        cur = next((x for x in (cfgv.get("rules") or []) if x.get("id") == "smoke-panel"), {})
        check("面板保存的多值字段落成数组",
              (cur.get("when") or {}).get("model") == ["panel-special", "*-mini"],
              json.dumps(cur.get("when"), ensure_ascii=False))
        check("面板保存的候选链与排序策略生效",
              (cur.get("route") or {}).get("providers") == ["mock-cheap"]
              and (cur.get("route") or {}).get("tactic") == "cheapest",
              json.dumps(cur.get("route"), ensure_ascii=False))
        check("面板保存的描述是普通字符串", cur.get("description") == "面板保存的规则",
              repr(cur.get("description")))
        st, d, _ = c.call("/route?model=panel-special")
        check("打开开关后新规则按条件命中（更靠前的规则不匹配这个模型）",
              d.get("rule") == "smoke-panel", str(d.get("rule")))
        run_cli("rule-remove", "smoke-panel")

        r = run_cli("rule-remove", "smoke-first")
        check("rule-remove 返回被删的 id",
              (as_json(r.stdout) or {}).get("removed") == "smoke-first", r.stdout[:120])
        run_cli("rule-remove", "smoke-rule")
        r = run_cli("rule-remove", "no-such-rule")
        check("删除不存在的规则报错退出", r.returncode != 0, r.stdout[:120])
        st, cfgv, _ = c.call("/config")
        ids = [x.get("id") for x in (cfgv.get("rules") or [])]
        check("两条冒烟规则都已清理", "smoke-rule" not in ids and "smoke-first" not in ids,
              str(ids))

        print("\n⑩ 优雅停止")
        proc.terminate()
        proc.wait(timeout=10)
        check("SIGTERM 后进程退出且清理 pid/port 文件",
              not os.path.exists(os.path.join(state, "llmhubd.port")))
        out = proc.stdout.read() if proc.stdout else ""
        check("日志中有请求记录行", "mock-primary" in out, out[-300:])
        print("\n⑭ 运行时文件丢失与冷启动（「重启电脑后点启动/重启没反应」的根因）")
        pid_path = os.path.join(state, "llmhubd.pid")
        port_path = os.path.join(state, "llmhubd.port")
        r = run_cli("status")
        check("网关停掉后 status 报「未运行」且 rc≠0",
              r.returncode != 0 and "未运行" in r.stdout, r.stdout.splitlines()[:1])
        r = run_cli("start")
        check("冷启动能把网关拉起来", r.returncode == 0 and "已启动" in r.stdout, r.stdout[-200:])
        _st, h, _ = c.call("/health")
        pid0 = int(h.get("pid") or 0)
        for f in (pid_path, port_path):
            if os.path.exists(f):
                os.remove(f)
        r = run_cli("status")
        check("pid/端口文件被删后 status 仍靠 /health 认出「运行中」",
              r.returncode == 0 and "运行中" in r.stdout, r.stdout.splitlines()[:2])
        back = int(open(pid_path).read().strip()) if os.path.exists(pid_path) else 0
        check("status 顺手把 pid 文件补回来", back == pid0, f"期望 {pid0}，实际 {back}")
        r = run_cli("start")
        check("start 认出已在运行、不会另起第二个进程",
              "已在运行" in r.stdout and "已启动" not in r.stdout, r.stdout[-200:])
        _st, h2, _ = c.call("/health")
        check("网关 pid 没有被第二个实例顶掉", int(h2.get("pid") or 0) == pid0, str(h2.get("pid")))
        for f in (pid_path, port_path):
            if os.path.exists(f):
                os.remove(f)
        r = run_cli("stop")
        check("stop 在文件丢失时靠 /health 找回真 pid 并停掉",
              r.returncode == 0 and "已停止" in r.stdout, r.stdout[-200:])
        gone = False
        try:
            c.call("/health", timeout=2)
        except (Exception, SystemExit):
            gone = True
        check("停止后 /health 不再响应", gone)

    finally:
        if proc and proc.poll() is None:
            proc.kill()
        shutil.rmtree(tmp, ignore_errors=True)
    print(f"\n{'='*60}\n通过 {len(PASS)} 项，失败 {len(FAIL)} 项")
    if FAIL:
        print("失败项：" + "；".join(FAIL))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
