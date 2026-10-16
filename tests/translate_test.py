#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""协议翻译测试：入站协议 ⇄ 上游协议 的双向转换。

本测试自带一个「假上游」（本机 HTTP 服务，分别模拟 Anthropic / Gemini /
OpenAI / Ollama 四种响应格式），再用临时配置把 llm-hub 网关指向它，
于是可以逐项断言：

  1. 上游真正收到的路径、鉴权头、请求体结构（正向翻译）
  2. 网关回给客户端的响应体（反向翻译）
  3. 流式（SSE / NDJSON）逐事件解析
  4. 工具调用与多模态在两套协议之间的转换

全程不需要外网、不需要任何真实 API key。

运行：python3 tests/translate_test.py
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DAEMON = os.path.join(ROOT, "daemon", "llmhubd.py")
UPSTREAM_PORT = 19191
GATEWAY_PORT = 18788

SEEN: list[dict] = []
SEEN_LOCK = threading.Lock()


def last_upstream(prefix: str) -> dict | None:
    with SEEN_LOCK:
        for item in reversed(SEEN):
            if item["path"].startswith(prefix):
                return item
    return None


def _content_text(messages) -> str:
    for m in reversed(messages or []):
        c = m.get("content")
        if isinstance(c, str):
            return c
        if isinstance(c, list):
            for part in c:
                if isinstance(part, dict) and part.get("text"):
                    return part["text"]
    return ""


class FakeUpstream(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):  # 静音
        pass

    # ── 基础输出 ────────────────────────────────────────────────────────────
    def _json(self, obj: dict):
        data = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _sse(self, payloads: list[dict]):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        for obj in payloads:
            line = ("data: " + json.dumps(obj, ensure_ascii=False) + "\n\n").encode()
            self.wfile.write(b"%x\r\n" % len(line) + line + b"\r\n")
        self.wfile.write(b"0\r\n\r\n")

    def _ndjson(self, objs: list[dict]):
        data = b"".join((json.dumps(o, ensure_ascii=False) + "\n").encode() for o in objs)
        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    # ── 分发 ────────────────────────────────────────────────────────────────
    def do_POST(self):  # noqa: N802
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n) if n else b""
        try:
            body = json.loads(raw.decode("utf-8", "replace") or "{}")
        except Exception:
            body = {"_raw": raw.decode("utf-8", "replace")}
        with SEEN_LOCK:
            SEEN.append({"path": self.path, "headers": dict(self.headers), "body": body})

        path = urllib.parse.urlsplit(self.path).path
        if path == "/v1/messages":
            return self._anthropic(body)
        if path.endswith(":streamGenerateContent"):
            return self._gemini(body, stream=True)
        if path.endswith(":generateContent"):
            return self._gemini(body, stream=False)
        if path == "/v1/chat/completions":
            return self._openai(body)
        if path == "/api/chat":
            return self._ollama(body)
        if path == "/v1/embeddings":
            return self._json({"object": "list", "model": body.get("model"),
                               "data": [{"object": "embedding", "index": 0,
                                         "embedding": [0.25, 0.5]}],
                               "usage": {"prompt_tokens": 2, "total_tokens": 2}})
        return self._json({"error": {"message": "未知上游路径 " + path}})

    # ── 四种上游协议 ────────────────────────────────────────────────────────
    def _anthropic(self, body: dict):
        text = "anthropic：" + _content_text(body.get("messages"))
        if body.get("stream"):
            return self._sse([
                {"type": "message_start", "message": {
                    "id": "msg_up", "type": "message", "role": "assistant",
                    "model": body.get("model"), "content": [],
                    "usage": {"input_tokens": 9, "output_tokens": 0}}},
                {"type": "content_block_start", "index": 0,
                 "content_block": {"type": "text", "text": ""}},
                {"type": "content_block_delta", "index": 0,
                 "delta": {"type": "text_delta", "text": text}},
                {"type": "content_block_stop", "index": 0},
                {"type": "message_delta", "delta": {"stop_reason": "end_turn"},
                 "usage": {"output_tokens": 4}},
                {"type": "message_stop"},
            ])
        return self._json({"id": "msg_up", "type": "message", "role": "assistant",
                           "model": body.get("model"),
                           "content": [{"type": "text", "text": text}],
                           "stop_reason": "end_turn",
                           "usage": {"input_tokens": 9, "output_tokens": 4}})

    def _gemini(self, body: dict, stream: bool):
        if stream:
            return self._sse([
                {"candidates": [{"content": {"role": "model",
                                             "parts": [{"text": "gemini-"}]}}]},
                {"candidates": [{"content": {"role": "model", "parts": [{"text": "流"}]},
                                 "finishReason": "STOP"}],
                 "usageMetadata": {"promptTokenCount": 6, "candidatesTokenCount": 2}},
            ])
        return self._json({
            "candidates": [{"content": {"role": "model", "parts": [{"text": "gemini-回答"}]},
                            "finishReason": "STOP"}],
            "usageMetadata": {"promptTokenCount": 6, "candidatesTokenCount": 2}})

    def _openai(self, body: dict):
        text = "openai：" + _content_text(body.get("messages"))
        if body.get("stream"):
            return self._sse([
                {"id": "chatcmpl_up", "object": "chat.completion.chunk",
                 "model": body.get("model"),
                 "choices": [{"index": 0, "delta": {"role": "assistant",
                                                    "content": text}}]},
                {"id": "chatcmpl_up", "object": "chat.completion.chunk",
                 "model": body.get("model"),
                 "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                 "usage": {"prompt_tokens": 3, "completion_tokens": 5}},
            ])
        return self._json({"id": "chatcmpl_up", "object": "chat.completion",
                           "model": body.get("model"),
                           "choices": [{"index": 0, "finish_reason": "stop",
                                        "message": {"role": "assistant", "content": text}}],
                           "usage": {"prompt_tokens": 3, "completion_tokens": 5,
                                     "total_tokens": 8}})

    def _ollama(self, body: dict):
        if body.get("stream"):
            return self._ndjson([
                {"message": {"role": "assistant", "content": "ollama-"}, "done": False},
                {"message": {"role": "assistant", "content": "流"}, "done": False},
                {"done": True, "prompt_eval_count": 7, "eval_count": 3},
            ])
        return self._json({"model": body.get("model"), "done": True,
                           "message": {"role": "assistant", "content": "ollama-回答"},
                           "prompt_eval_count": 7, "eval_count": 3})


# ── 测试配置：四个 provider 全部指向假上游 ──────────────────────────────────
CONFIG = {
    "version": 1,
    "server": {"host": "127.0.0.1", "port": GATEWAY_PORT, "api_key": "",
               "request_timeout_ms": 30000, "log_requests": True, "max_log_entries": 200},
    "providers": [
        {"id": "up-anthropic", "type": "anthropic", "enabled": True, "priority": 10,
         "base_url": f"http://127.0.0.1:{UPSTREAM_PORT}", "api_key": "sk-ant-test",
         "models": ["claude-*"], "params": {"anthropic_version": "2023-06-01"}},
        {"id": "up-gemini", "type": "gemini", "enabled": True, "priority": 20,
         "base_url": f"http://127.0.0.1:{UPSTREAM_PORT}", "api_key": "gm-test",
         "models": ["gemini-*"]},
        {"id": "up-openai", "type": "openai", "enabled": True, "priority": 30,
         "base_url": f"http://127.0.0.1:{UPSTREAM_PORT}", "api_key": "sk-up-test",
         "models": ["gpt-*"]},
        {"id": "up-ollama", "type": "ollama", "enabled": True, "priority": 40,
         "base_url": f"http://127.0.0.1:{UPSTREAM_PORT}", "auth": {"type": "none"},
         "models": ["llama*"]},
    ],
    "rules": [],
    "default_route": {"providers": ["*"], "tactic": "priority", "fallback": True, "retries": 0},
    "aliases": {},
    "scenarios": {},
    "guardrails": {"max_prompt_chars": 0, "block_patterns": [], "mask_response": []},
}

PASS: list[str] = []
FAIL: list[str] = []


def check(name: str, ok: bool, detail: str = ""):
    (PASS if ok else FAIL).append(name)
    mark = "\033[32m✓\033[0m" if ok else "\033[31m✗\033[0m"
    print(f"  {mark} {name}" + (f"\n      {detail}" if not ok and detail else ""))


def post(path: str, body: dict | None = None, headers: dict | None = None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(f"http://127.0.0.1:{GATEWAY_PORT}{path}", data=data,
                                 headers={"Content-Type": "application/json",
                                          **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, json.loads(r.read().decode() or "{}"), dict(r.headers)
    except urllib.error.HTTPError as e:
        raw = e.read().decode()
        try:
            return e.code, json.loads(raw or "{}"), dict(e.headers)
        except Exception:
            return e.code, {"_raw": raw}, dict(e.headers)


def stream(path: str, body: dict, headers: dict | None = None):
    data = json.dumps({**body, "stream": True}).encode()
    req = urllib.request.Request(f"http://127.0.0.1:{GATEWAY_PORT}{path}", data=data,
                                 headers={"Content-Type": "application/json",
                                          **(headers or {})})
    events: list[dict] = []
    with urllib.request.urlopen(req, timeout=30) as r:
        for raw_line in r:
            line = raw_line.decode("utf-8", "replace").strip()
            if line.startswith("data:"):
                payload = line[5:].strip()
                if payload and payload != "[DONE]":
                    try:
                        events.append(json.loads(payload))
                    except Exception:
                        pass
    return events


def wait_ready(timeout=20.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{GATEWAY_PORT}/health", timeout=2).read()
            return
        except Exception:
            time.sleep(0.2)
    raise SystemExit("网关未就绪")


def main() -> int:
    upstream = ThreadingHTTPServer(("127.0.0.1", UPSTREAM_PORT), FakeUpstream)
    threading.Thread(target=upstream.serve_forever, daemon=True).start()

    tmp = tempfile.mkdtemp(prefix="llmhub-translate-")
    cfg_path = os.path.join(tmp, "config.json")
    state_dir = os.path.join(tmp, "state")
    os.makedirs(state_dir, exist_ok=True)
    with open(cfg_path, "w", encoding="utf-8") as f:
        json.dump(CONFIG, f, ensure_ascii=False)
    proc = subprocess.Popen(
        [sys.executable, DAEMON, "--config", cfg_path, "--data-dir", state_dir, "serve"],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        env={**os.environ, "LLMHUB_DATA_DIR": state_dir})
    try:
        wait_ready()
        print(f"假上游 127.0.0.1:{UPSTREAM_PORT} · 网关 127.0.0.1:{GATEWAY_PORT}\n")

        # ① OpenAI 入站 → Anthropic 上游
        print("① OpenAI 入站 → Anthropic 上游")
        st, resp, _ = post("/v1/chat/completions", {
            "model": "claude-sonnet-4-x", "max_tokens": 128, "temperature": 0.3,
            "messages": [{"role": "system", "content": "你是助手"},
                         {"role": "user", "content": "你好"}]})
        up = last_upstream("/v1/messages")
        check("上游路径为 /v1/messages", up is not None)
        hdr = {k.lower(): v for k, v in (up or {}).get("headers", {}).items()}
        check("鉴权头 x-api-key 正确", hdr.get("x-api-key") == "sk-ant-test", json.dumps(hdr))
        check("带上 anthropic-version", hdr.get("anthropic-version") == "2023-06-01")
        body = (up or {}).get("body", {})
        check("system 提到顶层字段", body.get("system") == "你是助手",
              json.dumps(body, ensure_ascii=False)[:200])
        check("messages 转成 Anthropic 结构（content 为数组）",
              isinstance((body.get("messages") or [{}])[-1].get("content"), list),
              json.dumps(body.get("messages"), ensure_ascii=False)[:200])
        check("max_tokens / temperature 透传",
              body.get("max_tokens") == 128 and body.get("temperature") == 0.3)
        check("Anthropic 响应翻回 OpenAI 结构",
              st == 200 and resp.get("object") == "chat.completion"
              and resp["choices"][0]["message"]["content"].startswith("anthropic："),
              json.dumps(resp, ensure_ascii=False)[:200])
        check("usage 字段映射正确", (resp.get("usage") or {}).get("prompt_tokens") == 9
              and (resp.get("usage") or {}).get("completion_tokens") == 4,
              json.dumps(resp.get("usage"), ensure_ascii=False))

        # ② 工具定义：OpenAI tools → Anthropic tools
        print("\n② 工具定义翻译（OpenAI tools → Anthropic tools）")
        post("/v1/chat/completions", {
            "model": "claude-sonnet-4-x",
            "messages": [{"role": "user", "content": "现在几点"}],
            "tools": [{"type": "function", "function": {
                "name": "get_time", "description": "取时间",
                "parameters": {"type": "object",
                               "properties": {"tz": {"type": "string"}}}}}],
            "tool_choice": {"type": "auto"}})
        body = (last_upstream("/v1/messages") or {}).get("body", {})
        tool = (body.get("tools") or [{}])[0]
        check("tools 转成 name / description / input_schema",
              tool.get("name") == "get_time" and tool.get("description") == "取时间"
              and (tool.get("input_schema") or {}).get("type") == "object",
              json.dumps(body.get("tools"), ensure_ascii=False)[:250])
        check("tool_choice 转成 Anthropic 形式",
              (body.get("tool_choice") or {}).get("type") == "auto",
              json.dumps(body.get("tool_choice"), ensure_ascii=False))

        # ③ OpenAI 入站 → Gemini 上游
        print("\n③ OpenAI 入站 → Gemini 上游")
        st, resp, _ = post("/v1/chat/completions", {
            "model": "gemini-2.5-pro", "max_tokens": 256,
            "messages": [{"role": "system", "content": "简洁回答"},
                         {"role": "user", "content": "你好"},
                         {"role": "assistant", "content": "在"},
                         {"role": "user", "content": "继续"}]})
        up = last_upstream("/v1beta/models/")
        up_path = (up or {}).get("path", "")
        check("上游路径含 :generateContent 与 key 查询参数",
              ":generateContent" in up_path and "key=gm-test" in up_path, up_path)
        body = (up or {}).get("body", {})
        check("contents 使用 user/model 角色并交替",
              [c.get("role") for c in body.get("contents") or []] == ["user", "model", "user"],
              json.dumps(body.get("contents"), ensure_ascii=False)[:250])
        check("systemInstruction 已生成",
              ((body.get("systemInstruction") or {}).get("parts") or [{}])[0].get("text") == "简洁回答")
        check("generationConfig.maxOutputTokens 已生成",
              (body.get("generationConfig") or {}).get("maxOutputTokens") == 256,
              json.dumps(body.get("generationConfig"), ensure_ascii=False))
        check("Gemini 响应翻回 OpenAI 结构",
              resp.get("choices", [{}])[0].get("message", {}).get("content") == "gemini-回答",
              json.dumps(resp, ensure_ascii=False)[:200])

        # ③b 工具强制调用：required → Gemini toolConfig
        print("\n③b 工具强制调用（tool_choice=required → Gemini toolConfig）")
        post("/v1/chat/completions", {
            "model": "gemini-2.5-pro", "messages": [{"role": "user", "content": "必须调用"}],
            "tool_choice": "required",
            "tools": [{"type": "function", "function": {
                "name": "get_time",
                "parameters": {"type": "object", "properties": {}}}}]})
        tcfg = ((last_upstream("/v1beta/models/") or {}).get("body", {}) or {}).get("toolConfig") or {}
        check("required 转成 Gemini functionCallingConfig.mode=ANY",
              (tcfg.get("functionCallingConfig") or {}).get("mode") == "ANY",
              json.dumps(tcfg, ensure_ascii=False))

        # ④ Gemini 流式 → OpenAI chunk
        print("\n④ Gemini 流式（alt=sse）→ OpenAI chunk")
        events = stream("/v1/chat/completions",
                        {"model": "gemini-2.5-pro", "messages": [{"role": "user", "content": "流"}]})
        up_path = (last_upstream("/v1beta/models/") or {}).get("path", "")
        text = "".join((e.get("choices") or [{}])[0].get("delta", {}).get("content") or ""
                       for e in events if e.get("object") == "chat.completion.chunk")
        check("上游使用 :streamGenerateContent 且 alt=sse",
              ":streamGenerateContent" in up_path and "alt=sse" in up_path, up_path)
        check("客户端收到 OpenAI 风格 chunk 且文本完整",
              text == "gemini-流\n" or text == "gemini-流", repr(text))

        # ⑤ Anthropic 入站 → Gemini 上游 → Anthropic 响应
        print("\n⑤ Anthropic 入站 → Gemini 上游 → Anthropic 响应")
        st, resp, _ = post("/v1/messages", {
            "model": "gemini-2.5-flash", "max_tokens": 64, "system": "系统提示",
            "messages": [{"role": "user", "content": "你好"}]})
        check("回给客户端的是 Anthropic 结构",
              resp.get("type") == "message" and resp.get("role") == "assistant"
              and (resp.get("content") or [{}])[0].get("type") == "text",
              json.dumps(resp, ensure_ascii=False)[:250])
        check("stop_reason 已映射", resp.get("stop_reason") == "end_turn",
              str(resp.get("stop_reason")))
        check("Anthropic usage 反向映射",
              (resp.get("usage") or {}).get("input_tokens") == 6
              and (resp.get("usage") or {}).get("output_tokens") == 2,
              json.dumps(resp.get("usage"), ensure_ascii=False))

        # ⑥ 工具结果回传：OpenAI 上游
        print("\n⑥ 工具结果回传（tool_calls / role=tool → OpenAI 上游）")
        post("/v1/chat/completions", {
            "model": "gpt-4o",
            "messages": [
                {"role": "user", "content": "调用工具"},
                {"role": "assistant", "tool_calls": [
                    {"id": "call_1", "type": "function",
                     "function": {"name": "get_time", "arguments": "{\"tz\":\"UTC\"}"}}]},
                {"role": "tool", "tool_call_id": "call_1", "content": "12:00"}]})
        msgs = ((last_upstream("/v1/chat/completions") or {}).get("body", {})).get("messages") or []
        check("上游收到 assistant.tool_calls",
              any(m.get("tool_calls") for m in msgs if m.get("role") == "assistant"),
              json.dumps(msgs, ensure_ascii=False)[:250])
        check("上游收到 role=tool 且带 tool_call_id",
              any(m.get("role") == "tool" and m.get("tool_call_id") == "call_1" for m in msgs),
              json.dumps(msgs, ensure_ascii=False)[:250])
        check("工具参数保持 JSON 字符串",
              any((m.get("tool_calls") or [{}])[0].get("function", {}).get("arguments")
                  == "{\"tz\": \"UTC\"}" or
                  (m.get("tool_calls") or [{}])[0].get("function", {}).get("arguments")
                  == '{"tz":"UTC"}'
                  for m in msgs if m.get("tool_calls")),
              json.dumps(msgs, ensure_ascii=False)[:250])

        # ⑦ 多模态：data URL → Anthropic image block
        print("\n⑦ 多模态（data URL 图片 → Anthropic image/base64）")
        png = ("data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJ"
               "AAAADUlEQVR42mP8z8AAAwAB/AGtPj4AAAAASUVORK5CYII=")
        post("/v1/chat/completions", {
            "model": "claude-sonnet-4-x",
            "messages": [{"role": "user", "content": [
                {"type": "text", "text": "看图"},
                {"type": "image_url", "image_url": {"url": png}}]}]})
        parts = (((last_upstream("/v1/messages") or {}).get("body", {})
                  .get("messages") or [{}])[0].get("content") or [])
        check("图片转成 Anthropic image/source(type=base64)",
              any(isinstance(p, dict) and p.get("type") == "image"
                  and (p.get("source") or {}).get("type") == "base64"
                  and (p.get("source") or {}).get("media_type") == "image/png" for p in parts),
              json.dumps(parts, ensure_ascii=False)[:250])

        # ⑧ Ollama：NDJSON 流式与非流式
        print("\n⑧ Ollama（/api/chat 与 NDJSON 解析）")
        st, resp, _ = post("/v1/chat/completions",
                           {"model": "llama3.1", "messages": [{"role": "user", "content": "你好"}]})
        up = last_upstream("/api/chat")
        check("上游路径为 /api/chat 且不带鉴权头",
              up is not None and not (up.get("headers") or {}).get("Authorization"),
              json.dumps((up or {}).get("headers"), ensure_ascii=False))
        check("Ollama 非流式响应翻回 OpenAI",
              resp.get("choices", [{}])[0].get("message", {}).get("content") == "ollama-回答",
              json.dumps(resp, ensure_ascii=False)[:200])
        check("Ollama 用量字段被映射（prompt_eval_count/eval_count）",
              (resp.get("usage") or {}).get("prompt_tokens") == 7
              and (resp.get("usage") or {}).get("completion_tokens") == 3,
              json.dumps(resp.get("usage"), ensure_ascii=False))
        events = stream("/v1/chat/completions",
                        {"model": "llama3.1", "messages": [{"role": "user", "content": "流"}]})
        text = "".join((e.get("choices") or [{}])[0].get("delta", {}).get("content") or ""
                       for e in events if e.get("object") == "chat.completion.chunk")
        check("Ollama NDJSON 流被拼成完整回答", text == "ollama-流", repr(text))
        events = stream("/v1/chat/completions",
                        {"model": "gpt-4o", "messages": [{"role": "user", "content": "流"}]})
        text = "".join((e.get("choices") or [{}])[0].get("delta", {}).get("content") or ""
                       for e in events if e.get("object") == "chat.completion.chunk")
        ups_body = (last_upstream("/v1/chat/completions") or {}).get("body", {})
        check("OpenAI 流式请求带 stream_options.include_usage",
              (ups_body.get("stream_options") or {}).get("include_usage") is True
              and ups_body.get("stream") is True,
              json.dumps(ups_body, ensure_ascii=False)[:200])
        check("OpenAI SSE 流被翻成客户端 chunk", text.startswith("openai："), repr(text))

        # ⑨ 其它端点
        print("\n⑨ 其它端点（count_tokens / embeddings / Responses）")
        st, resp, _ = post("/v1/messages/count_tokens",
                           {"model": "gemini-2.5-flash",
                            "messages": [{"role": "user", "content": "数一下"}]})
        check("count_tokens 返回 input_tokens",
              st == 200 and (resp.get("input_tokens") or 0) > 0,
              json.dumps(resp, ensure_ascii=False)[:150])
        st, resp, _ = post("/v1/embeddings", {"model": "gpt-4o", "input": "hello"})
        check("embeddings 转发到上游",
              st == 200 and (resp.get("data") or [{}])[0].get("embedding") == [0.25, 0.5],
              json.dumps(resp, ensure_ascii=False)[:150])
        st, resp, _ = post("/v1/responses", {"model": "gemini-2.5-flash", "input": "responses 协议"})
        check("Responses 协议落到同一路由并回 Responses 结构",
              st == 200 and resp.get("object") == "response"
              and resp.get("output_text") == "gemini-回答",
              json.dumps(resp, ensure_ascii=False)[:200])

        # ⑩ 路由头与协议无关性
        print("\n⑩ 路由头与响应头")
        st, resp, hdrs = post("/v1/chat/completions",
                              {"model": "gpt-4o", "messages": [{"role": "user", "content": "hi"}]},
                              {"X-LLMHub-Provider": "up-anthropic"})
        check("X-LLMHub-Provider 能强制指定上游",
              hdrs.get("X-LLMHub-Provider") == "up-anthropic", json.dumps(hdrs, ensure_ascii=False))
        check("响应头回带 model/rule/tactic",
              bool(hdrs.get("X-LLMHub-Model")) and bool(hdrs.get("X-LLMHub-Rule"))
              and bool(hdrs.get("X-LLMHub-Tactic")), json.dumps(hdrs, ensure_ascii=False))
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except Exception:
            proc.kill()
        upstream.shutdown()
        shutil.rmtree(tmp, ignore_errors=True)

    print("\n" + "=" * 60)
    print(f"通过 {len(PASS)} 项，失败 {len(FAIL)} 项")
    if FAIL:
        print("失败项：")
        for name in FAIL:
            print("  - " + name)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
