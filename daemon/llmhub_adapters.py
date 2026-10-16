#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""llmhub_adapters —— 上游 provider 适配器与 HTTP 客户端。

全部同步实现（stdlib http.client），由服务端放进线程池执行，流式则通过线程 + 队列
泵到 SSE 响应。支持类型：openai（含一切 OpenAI 兼容端点）、anthropic、gemini、
ollama、mock（内置模拟，用于测试与无 key 演示）。
"""
from __future__ import annotations

import gzip
import json
import re
import socket
import ssl
import time
import zlib
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Iterator
from urllib.parse import urlsplit, urlunsplit

from llmhub_canon import (CanonRequest, CanonResponse, Msg, Part, new_id,
                         anthropic_tool_choice, gemini_tool_choice, openai_tool_choice)


# ── 错误与 HTTP 基础 ────────────────────────────────────────────────────────


class UpstreamError(Exception):
    def __init__(self, message: str, status: int | None = None, body: str = "",
                 retryable: bool = True, kind: str = "error"):
        super().__init__(message)
        self.status = status
        self.body = (body or "")[:2000]
        self.retryable = retryable
        self.kind = kind            # error | auth | rate_limit | timeout | connect | bad_request


class ConfigError(Exception):
    """provider 配置本身有问题（缺 key、缺 base_url 等）。"""


@dataclass
class HttpRequest:
    method: str = "POST"
    url: str = ""
    headers: dict = field(default_factory=dict)
    body: bytes | None = None
    timeout_ms: int = 60000


def _conn_for(url: str, timeout: float, verify_tls: bool = True):
    import http.client

    u = urlsplit(url)
    if u.scheme not in ("http", "https"):
        raise UpstreamError(f"不支持的 URL scheme: {url!r}", kind="connect")
    if u.scheme == "https":
        ctx = ssl.create_default_context()
        if not verify_tls:
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
        conn = http.client.HTTPSConnection(u.hostname, u.port or 443, timeout=timeout, context=ctx)
    else:
        conn = http.client.HTTPConnection(u.hostname, u.port or 80, timeout=timeout)
    path = u.path or "/"
    if u.query:
        path += "?" + u.query
    return conn, path


def _decode_body(raw: bytes, headers) -> bytes:
    enc = (headers.get("content-encoding") or "").lower()
    try:
        if enc == "gzip":
            return gzip.decompress(raw)
        if enc == "deflate":
            return zlib.decompress(raw)
    except Exception:
        return raw
    return raw


def http_request(req: HttpRequest, verify_tls: bool = True):
    """发一个请求，返回 (status, headers, body_bytes)。"""
    conn, path = _conn_for(req.url, max(req.timeout_ms, 100) / 1000.0, verify_tls)
    try:
        conn.request(req.method, path, body=req.body, headers=req.headers or {})
        resp = conn.getresponse()
        raw = resp.read()
        hdrs = {k.lower(): v for k, v in resp.getheaders()}
        return resp.status, hdrs, _decode_body(raw, hdrs)
    except socket.timeout as e:
        raise UpstreamError(f"连接超时: {e}", kind="timeout") from e
    except (OSError, ssl.SSLError) as e:
        raise UpstreamError(f"连接失败: {e}", kind="connect") from e
    finally:
        try:
            conn.close()
        except Exception:
            pass


class LineStream:
    """上游流式响应的行迭代器（必须 close）。"""

    def __init__(self, req: HttpRequest, verify_tls: bool = True):
        self.req = req
        self.verify_tls = verify_tls
        self.conn = None
        self.resp = None
        self.status = 0
        self.headers: dict = {}

    def open(self) -> "LineStream":
        self.conn, path = _conn_for(self.req.url, max(self.req.timeout_ms, 100) / 1000.0,
                                    self.verify_tls)
        try:
            self.conn.request(self.req.method, path, body=self.req.body,
                              headers=self.req.headers or {})
            self.resp = self.conn.getresponse()
        except socket.timeout as e:
            self.close()
            raise UpstreamError(f"连接超时: {e}", kind="timeout") from e
        except (OSError, ssl.SSLError) as e:
            self.close()
            raise UpstreamError(f"连接失败: {e}", kind="connect") from e
        self.status = self.resp.status
        self.headers = {k.lower(): v for k, v in self.resp.getheaders()}
        return self

    def lines(self) -> Iterator[str]:
        """逐行 yield 文本（已 strip 尾部换行；空行也保留，SSE 需要）。"""
        assert self.resp is not None
        try:
            while True:
                raw = self.resp.readline()
                if not raw:
                    break
                yield raw.decode("utf-8", errors="replace").rstrip("\r\n")
        except socket.timeout as e:
            raise UpstreamError(f"读取超时: {e}", kind="timeout") from e
        except (OSError, ssl.SSLError) as e:
            raise UpstreamError(f"读取失败: {e}", kind="connect") from e

    def read(self) -> bytes:
        assert self.resp is not None
        try:
            return _decode_body(self.resp.read(), self.headers)
        except (OSError, ssl.SSLError) as e:
            raise UpstreamError(f"读取失败: {e}", kind="connect") from e

    def close(self):
        try:
            if self.resp is not None:
                self.resp.close()
        except Exception:
            pass
        try:
            if self.conn is not None:
                self.conn.close()
        except Exception:
            pass
        self.resp = None
        self.conn = None


def check_status(status: int, body: bytes):
    """非 2xx 抛出携带上游错误信息的 UpstreamError。"""
    if 200 <= status < 300:
        return
    text = body.decode("utf-8", errors="replace") if isinstance(body, bytes) else str(body)
    msg = ""
    try:
        j = json.loads(text)
        err = j.get("error") if isinstance(j, dict) else None
        if isinstance(err, dict):
            msg = str(err.get("message") or err.get("type") or "")
        elif isinstance(err, str):
            msg = err
        elif isinstance(j, dict):
            msg = str(j.get("message") or j.get("detail") or "")
    except Exception:
        msg = text[:300]
    kind = "error"
    retryable = True
    if status in (401, 403):
        kind, retryable = "auth", True
    elif status == 429:
        kind, retryable = "rate_limit", True
    elif status in (400, 404, 422):
        kind, retryable = "bad_request", True      # 也可能是模型不支持 → 允许换 provider
    elif status == 408:
        kind, retryable = "timeout", True
    elif status >= 500:
        kind, retryable = "error", True
    raise UpstreamError(f"上游 HTTP {status}: {msg or text[:200]}", status=status,
                        body=text, retryable=retryable, kind=kind)


def sse_events(lines: Iterable[str]) -> Iterator[tuple[str, str]]:
    """解析 SSE，yield (event_name, data)。兼容无 event 名的 OpenAI 风格。"""
    event = ""
    data_parts: list[str] = []
    for line in lines:
        if line == "":
            if data_parts:
                yield event, "\n".join(data_parts)
            event, data_parts = "", []
            continue
        if line.startswith(":"):
            continue
        if line.startswith("event:"):
            event = line[6:].strip()
        elif line.startswith("data:"):
            data_parts.append(line[5:].lstrip())
        elif line.startswith("{"):        # 少数端点直接吐裸 JSON（ndjson 风格 SSE）
            if data_parts:
                yield event, "\n".join(data_parts)
                event, data_parts = "", []
            yield "", line
    if data_parts:
        yield event, "\n".join(data_parts)


def json_lines(lines: Iterable[str]) -> Iterator[dict]:
    """NDJSON：每行一个 JSON 对象（ollama 流式）。"""
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except Exception:
            continue
        if isinstance(obj, dict):
            yield obj


# ── 规范 → 各协议的消息转换 ────────────────────────────────────────────────


def _tool_result_text(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        out = []
        for b in content:
            if isinstance(b, str):
                out.append(b)
            elif isinstance(b, dict):
                if b.get("type") == "text":
                    out.append(b.get("text", ""))
                elif b.get("type") == "image":
                    out.append("[image]")
                else:
                    out.append(json.dumps(b, ensure_ascii=False))
        return "\n".join(out)
    return json.dumps(content, ensure_ascii=False)


def to_openai_messages(req: CanonRequest) -> list[dict]:
    out: list[dict] = []
    for m in req.messages:
        if m.role == "system":
            if m.text():
                out.append({"role": "system", "content": m.text()})
            continue
        tool_results = [p for p in m.parts if p.type == "tool_result"]
        for p in tool_results:
            out.append({"role": "tool", "tool_call_id": p.tool_use_id or "call",
                        "content": _tool_result_text(p.content)})
        if m.role == "tool" and not tool_results:
            out.append({"role": "tool", "tool_call_id": m.tool_call_id or "call",
                        "content": m.text()})
            continue
        blocks: list[dict] = []
        tool_calls: list[dict] = []
        plain_text: list[str] = []
        for p in m.parts:
            if p.type == "text":
                plain_text.append(p.text)
                blocks.append({"type": "text", "text": p.text})
            elif p.type == "image":
                url = p.image_url or (f"data:{p.media_type or 'image/png'};base64,{p.data}"
                                      if p.data else "")
                if url:
                    blocks.append({"type": "image_url", "image_url": {"url": url}})
            elif p.type == "tool_use":
                tool_calls.append({
                    "id": p.tool_use_id or new_id("call"),
                    "type": "function",
                    "function": {"name": p.name,
                                 "arguments": p.input if isinstance(p.input, str)
                                 else json.dumps(p.input or {}, ensure_ascii=False)},
                })
        has_non_text = any(b["type"] != "text" for b in blocks)
        if m.role == "assistant" and not tool_calls and not has_non_text:
            out.append({"role": "assistant", "content": "\n".join(plain_text)})
        elif blocks or tool_calls:
            msg: dict = {"role": m.role,
                         "content": (blocks if has_non_text else ("\n".join(plain_text) or None))}
            if tool_calls:
                msg["tool_calls"] = tool_calls
                if not plain_text:
                    msg["content"] = msg["content"] or None
            out.append(msg)
    if not out:
        out.append({"role": "user", "content": ""})
    return out


def to_openai_tools(req: CanonRequest) -> list[dict]:
    return [{"type": "function",
             "function": {"name": t["name"], "description": t.get("description", ""),
                          "parameters": t.get("parameters") or {"type": "object", "properties": {}}}}
            for t in req.tools if t.get("name")]


def to_anthropic_messages(req: CanonRequest) -> list[dict]:
    """Anthropic 要求 user/assistant 交替，tool_result 必须放在 user 消息里。"""
    out: list[dict] = []
    for m in req.messages:
        if m.role == "system":
            continue
        blocks: list[dict] = []
        for p in m.parts:
            if p.type == "text":
                if p.text:
                    blocks.append({"type": "text", "text": p.text})
            elif p.type == "image":
                if p.data:
                    blocks.append({"type": "image", "source": {
                        "type": "base64", "media_type": p.media_type or "image/png",
                        "data": p.data}})
                elif p.image_url.startswith("data:"):
                    head, _, b64 = p.image_url.partition(",")
                    media = head.split(";")[0].replace("data:", "") or "image/png"
                    blocks.append({"type": "image", "source": {
                        "type": "base64", "media_type": media, "data": b64}})
            elif p.type == "tool_use":
                blocks.append({"type": "tool_use", "id": p.tool_use_id or new_id("toolu"),
                               "name": p.name, "input": p.input if isinstance(p.input, dict)
                               else {"_raw": p.input}})
            elif p.type == "tool_result":
                blocks.append({"type": "tool_result", "tool_use_id": p.tool_use_id or "call",
                               "content": _tool_result_text(p.content)})
        if not blocks:
            continue
        role = "assistant" if m.role == "assistant" else "user"
        if out and out[-1]["role"] == role:
            out[-1]["content"].extend(blocks)
        else:
            out.append({"role": role, "content": blocks})
    if not out:
        out.append({"role": "user", "content": [{"type": "text", "text": ""}]})
    if out[0]["role"] != "user":
        out.insert(0, {"role": "user", "content": [{"type": "text", "text": "(continue)"}]})
    return out


def to_anthropic_tools(req: CanonRequest) -> list[dict]:
    return [{"name": t["name"], "description": t.get("description", ""),
             "input_schema": t.get("parameters") or {"type": "object", "properties": {}}}
            for t in req.tools if t.get("name")]


def to_gemini_contents(req: CanonRequest) -> list[dict]:
    out: list[dict] = []
    for m in req.messages:
        if m.role == "system":
            continue
        parts: list[dict] = []
        for p in m.parts:
            if p.type == "text" and p.text:
                parts.append({"text": p.text})
            elif p.type == "image":
                if p.data:
                    parts.append({"inlineData": {"mimeType": p.media_type or "image/png",
                                                 "data": p.data}})
                elif p.image_url.startswith("data:"):
                    head, _, b64 = p.image_url.partition(",")
                    media = head.split(";")[0].replace("data:", "") or "image/png"
                    parts.append({"inlineData": {"mimeType": media, "data": b64}})
                elif p.image_url:
                    parts.append({"fileData": {"mimeType": "image/png", "fileUri": p.image_url}})
            elif p.type == "tool_use":
                parts.append({"functionCall": {"name": p.name, "args": p.input or {}}})
            elif p.type == "tool_result":
                parts.append({"functionResponse": {
                    "name": m.name or p.name or "tool",
                    "response": {"result": _tool_result_text(p.content)}}})
        if not parts:
            continue
        role = "model" if m.role == "assistant" else "user"
        if out and out[-1]["role"] == role:
            out[-1]["parts"].extend(parts)
        else:
            out.append({"role": role, "parts": parts})
    if not out:
        out.append({"role": "user", "parts": [{"text": ""}]})
    return out


# ── 适配器 ──────────────────────────────────────────────────────────────────

_RETRY_AFTER_RE = re.compile(r"try again in ([\d.]+)s", re.I)


class BaseAdapter:
    type = "base"

    def build(self, req: CanonRequest, provider: dict, model: str) -> HttpRequest:
        raise NotImplementedError

    def parse(self, body: bytes, req: CanonRequest, provider: dict,
              model: str) -> CanonResponse:
        raise NotImplementedError

    def parse_stream(self, lines: Iterable[str], req: CanonRequest, provider: dict,
                     model: str) -> Iterator[dict]:
        raise NotImplementedError

    # 供子类共用：把 provider 的鉴权头与自定义头合并
    @staticmethod
    def _headers(provider: dict) -> dict:
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        auth = provider.get("auth") or {}
        key = provider.get("api_key") or ""
        atype = (auth.get("type") or "bearer").lower()
        header_name = auth.get("header") or "Authorization"
        prefix = auth.get("prefix")
        if key:
            if atype == "bearer":
                headers[header_name] = f"{prefix if prefix is not None else 'Bearer '}{key}"
            elif atype == "header":
                headers[header_name] = key if prefix is None else f"{prefix}{key}"
            elif atype == "query":       # gemini: ?key=xxx
                pass
            elif atype == "none":
                pass
        for k, v in (provider.get("headers") or {}).items():
            if v is not None:
                headers[str(k)] = str(v)
        return headers


class OpenAIAdapter(BaseAdapter):
    type = "openai"

    def build(self, req: CanonRequest, provider: dict, model: str) -> HttpRequest:
        base = (provider.get("base_url") or "").rstrip("/")
        if not base:
            raise ConfigError("provider 缺少 base_url")
        path = provider.get("path") or "/v1/chat/completions"
        params = provider.get("params") or {}
        mt_field = params.get("max_tokens_field") or "max_tokens"
        body: dict[str, Any] = {
            "model": model,
            "messages": to_openai_messages(req),
        }
        if req.max_tokens:
            body[mt_field] = req.max_tokens
        if req.temperature is not None:
            body["temperature"] = req.temperature
        if req.top_p is not None:
            body["top_p"] = req.top_p
        if req.stop:
            body["stop"] = req.stop
        tools = to_openai_tools(req)
        if tools and params.get("no_tools") is not True:
            body["tools"] = tools
            tc = openai_tool_choice(req.tool_choice)
            if tc is not None:
                body["tool_choice"] = tc
        if req.stream:
            body["stream"] = True
            if params.get("stream_usage", True):
                body["stream_options"] = {"include_usage": True}
        for k, v in (params.get("extra_body") or {}).items():
            body[k] = v
        headers = self._headers(provider)
        if req.stream:
            headers["Accept"] = "text/event-stream"
        return HttpRequest(url=base + path, headers=headers,
                           body=json.dumps(body, ensure_ascii=False).encode(),
                           timeout_ms=int(provider.get("timeout_ms") or 60000))

    def parse(self, body: bytes, req, provider, model) -> CanonResponse:
        data = json.loads(body.decode("utf-8", "replace") or "{}")
        choice = (data.get("choices") or [{}])[0]
        msg = choice.get("message") or {}
        usage = data.get("usage") or {}
        tool_calls = []
        for tc in msg.get("tool_calls") or []:
            fn = tc.get("function") or {}
            tool_calls.append({"id": tc.get("id") or new_id("call"),
                               "name": fn.get("name", ""), "arguments": fn.get("arguments") or "{}"})
        return CanonResponse(
            id=str(data.get("id") or new_id()),
            model=str(data.get("model") or model),
            text=msg.get("content") or "",
            reasoning=msg.get("reasoning_content") or msg.get("reasoning") or "",
            tool_calls=tool_calls,
            finish_reason=str(choice.get("finish_reason") or "stop"),
            prompt_tokens=int(usage.get("prompt_tokens") or 0),
            completion_tokens=int(usage.get("completion_tokens") or 0),
            raw=data,
        )

    def parse_stream(self, lines, req, provider, model) -> Iterator[dict]:
        tool_index: dict[int, int] = {}
        for event, data in sse_events(lines):
            if data.strip() == "[DONE]":
                yield {"t": "done", "reason": "stop"}
                return
            try:
                obj = json.loads(data)
            except Exception:
                continue
            if isinstance(obj, dict) and obj.get("error"):
                err = obj["error"]
                raise UpstreamError(f"上游流内错误: {err.get('message') if isinstance(err, dict) else err}",
                                    kind="error")
            usage = obj.get("usage") or {}
            if usage:
                yield {"t": "usage", "prompt": int(usage.get("prompt_tokens") or 0),
                       "completion": int(usage.get("completion_tokens") or 0)}
            for choice in obj.get("choices") or []:
                delta = choice.get("delta") or {}
                if delta.get("reasoning_content"):
                    yield {"t": "reasoning", "text": delta["reasoning_content"]}
                if delta.get("content"):
                    yield {"t": "text", "text": delta["content"]}
                for tc in delta.get("tool_calls") or []:
                    idx = int(tc.get("index") or 0)
                    fn = tc.get("function") or {}
                    if idx not in tool_index:
                        tool_index[idx] = idx
                        yield {"t": "tool_start", "index": idx, "id": tc.get("id") or new_id("call"),
                               "name": fn.get("name") or ""}
                    if fn.get("arguments"):
                        yield {"t": "tool_args", "index": idx, "args": fn["arguments"]}
                if choice.get("finish_reason"):
                    yield {"t": "done", "reason": choice["finish_reason"]}
                    return
        yield {"t": "done", "reason": "stop"}


class AnthropicAdapter(BaseAdapter):
    type = "anthropic"

    def build(self, req: CanonRequest, provider: dict, model: str) -> HttpRequest:
        base = (provider.get("base_url") or "https://api.anthropic.com").rstrip("/")
        path = provider.get("path") or "/v1/messages"
        params = provider.get("params") or {}
        body: dict[str, Any] = {
            "model": model,
            "max_tokens": req.max_tokens or 1024,
            "messages": to_anthropic_messages(req),
        }
        if req.system:
            body["system"] = req.system
        if req.temperature is not None:
            body["temperature"] = req.temperature
        if req.top_p is not None:
            body["top_p"] = req.top_p
        if req.stop:
            body["stop_sequences"] = req.stop
        tools = to_anthropic_tools(req)
        if tools and params.get("no_tools") is not True:
            body["tools"] = tools
            tc = anthropic_tool_choice(req.tool_choice)
            if tc is not None:
                body["tool_choice"] = tc
        if req.stream:
            body["stream"] = True
        for k, v in (params.get("extra_body") or {}).items():
            body[k] = v
        headers = self._headers(provider)
        if "x-api-key" not in {k.lower() for k in headers}:
            key = provider.get("api_key") or ""
            if key:
                headers.pop("Authorization", None)
                headers["x-api-key"] = key
        headers.setdefault("anthropic-version", params.get("anthropic_version") or "2023-06-01")
        if req.stream:
            headers["Accept"] = "text/event-stream"
        return HttpRequest(url=base + path, headers=headers,
                           body=json.dumps(body, ensure_ascii=False).encode(),
                           timeout_ms=int(provider.get("timeout_ms") or 60000))

    def parse(self, body: bytes, req, provider, model) -> CanonResponse:
        data = json.loads(body.decode("utf-8", "replace") or "{}")
        text_parts, think_parts, tool_calls = [], [], []
        for block in data.get("content") or []:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "thinking":
                think_parts.append(block.get("thinking", ""))
            elif block.get("type") == "text":
                text_parts.append(block.get("text", ""))
            elif block.get("type") == "tool_use":
                tool_calls.append({"id": block.get("id") or new_id("toolu"),
                                   "name": block.get("name", ""),
                                   "arguments": json.dumps(block.get("input") or {},
                                                           ensure_ascii=False)})
        usage = data.get("usage") or {}
        return CanonResponse(
            id=str(data.get("id") or new_id("msg")),
            model=str(data.get("model") or model),
            text="".join(text_parts),
            reasoning="".join(think_parts),
            tool_calls=tool_calls,
            finish_reason=str(data.get("stop_reason") or "end_turn"),
            prompt_tokens=int(usage.get("input_tokens") or 0),
            completion_tokens=int(usage.get("output_tokens") or 0),
            raw=data,
        )

    def parse_stream(self, lines, req, provider, model) -> Iterator[dict]:
        tool_index = 0
        for event, data in sse_events(lines):
            try:
                obj = json.loads(data)
            except Exception:
                continue
            etype = obj.get("type") or event
            if etype == "error":
                err = obj.get("error") or {}
                raise UpstreamError(f"上游流内错误: {err.get('message') or data}", kind="error")
            if etype == "message_start":
                u = ((obj.get("message") or {}).get("usage") or {})
                yield {"t": "usage", "prompt": int(u.get("input_tokens") or 0), "completion": 0}
            elif etype == "content_block_start":
                cb = obj.get("content_block") or {}
                if cb.get("type") == "tool_use":
                    yield {"t": "tool_start", "index": tool_index, "id": cb.get("id") or new_id("toolu"),
                           "name": cb.get("name") or ""}
            elif etype == "content_block_delta":
                d = obj.get("delta") or {}
                if d.get("type") == "text_delta" and d.get("text"):
                    yield {"t": "text", "text": d["text"]}
                elif d.get("type") == "thinking_delta" and d.get("thinking"):
                    yield {"t": "reasoning", "text": d["thinking"]}
                elif d.get("type") == "input_json_delta" and d.get("partial_json"):
                    yield {"t": "tool_args", "index": tool_index, "args": d["partial_json"]}
            elif etype == "content_block_stop":
                tool_index += 1
            elif etype == "message_delta":
                u = obj.get("usage") or {}
                if u:
                    yield {"t": "usage", "prompt": 0, "completion": int(u.get("output_tokens") or 0)}
                reason = (obj.get("delta") or {}).get("stop_reason")
                if reason:
                    yield {"t": "done", "reason": reason}
                    return
        yield {"t": "done", "reason": "stop"}


class GeminiAdapter(BaseAdapter):
    type = "gemini"

    def build(self, req: CanonRequest, provider: dict, model: str) -> HttpRequest:
        base = (provider.get("base_url") or "https://generativelanguage.googleapis.com").rstrip("/")
        params = provider.get("params") or {}
        api_version = params.get("api_version") or "v1beta"
        verb = "streamGenerateContent" if req.stream else "generateContent"
        path = provider.get("path") or f"/{api_version}/models/{model}:{verb}"
        url = base + path
        key = provider.get("api_key") or ""
        auth_type = ((provider.get("auth") or {}).get("type") or "query").lower()
        if key and auth_type == "query" and "key=" not in url:
            url += ("&" if "?" in url else "?") + "key=" + key
        body: dict[str, Any] = {"contents": to_gemini_contents(req)}
        if req.system:
            body["systemInstruction"] = {"parts": [{"text": req.system}]}
        gen: dict[str, Any] = {"maxOutputTokens": req.max_tokens or 2048}
        if req.temperature is not None:
            gen["temperature"] = req.temperature
        if req.top_p is not None:
            gen["topP"] = req.top_p
        if req.stop:
            gen["stopSequences"] = req.stop
        body["generationConfig"] = gen
        tools = to_openai_tools(req)
        if tools and params.get("no_tools") is not True:
            decls = []
            for t in tools:
                fn = t["function"]
                decls.append({"name": fn["name"], "description": fn.get("description", ""),
                              "parameters": _gemini_schema(fn.get("parameters") or {})})
            body["tools"] = [{"functionDeclarations": decls}]
            tcfg = gemini_tool_choice(req.tool_choice)
            if tcfg:
                body["toolConfig"] = tcfg
        headers = self._headers(provider)
        if req.stream:
            headers["Accept"] = "text/event-stream"
        if req.stream and "alt=sse" not in url:
            url += ("&" if "?" in url else "?") + "alt=sse"
        return HttpRequest(url=url, headers=headers,
                           body=json.dumps(body, ensure_ascii=False).encode(),
                           timeout_ms=int(provider.get("timeout_ms") or 60000))

    def _scan_parts(self, obj: dict) -> Iterator[dict]:
        for cand in obj.get("candidates") or []:
            for part in ((cand.get("content") or {}).get("parts") or []):
                if not isinstance(part, dict):
                    continue
                if part.get("text"):
                    yield {"t": "text", "text": part["text"]}
                if part.get("functionCall"):
                    fc = part["functionCall"]
                    yield {"t": "tool_start", "index": 0, "id": new_id("call"),
                           "name": fc.get("name", "")}
                    yield {"t": "tool_args", "index": 0,
                           "args": json.dumps(fc.get("args") or {}, ensure_ascii=False)}
        um = obj.get("usageMetadata") or {}
        if um:
            yield {"t": "usage", "prompt": int(um.get("promptTokenCount") or 0),
                   "completion": int(um.get("candidatesTokenCount") or 0)}

    def parse(self, body: bytes, req, provider, model) -> CanonResponse:
        data = json.loads(body.decode("utf-8", "replace") or "{}")
        text, think, tool_calls = "", "", []
        finish = "stop"
        for cand in data.get("candidates") or []:
            for part in ((cand.get("content") or {}).get("parts") or []):
                if isinstance(part, dict):
                    if part.get("thought"):          # Gemini 用 thought=true 标记思考片段
                        think += part.get("text") or ""
                        continue
                    text += part.get("text") or ""
                    if part.get("functionCall"):
                        fc = part["functionCall"]
                        tool_calls.append({
                            "id": new_id("call"), "name": fc.get("name", ""),
                            "arguments": json.dumps(fc.get("args") or {}, ensure_ascii=False)})
            if cand.get("finishReason"):
                finish = str(cand["finishReason"]).lower()
        um = data.get("usageMetadata") or {}
        return CanonResponse(id=new_id("gemini"), model=model, text=text, reasoning=think,
                             tool_calls=tool_calls, finish_reason=finish,
                             prompt_tokens=int(um.get("promptTokenCount") or 0),
                             completion_tokens=int(um.get("candidatesTokenCount") or 0),
                             raw=data)

    def parse_stream(self, lines, req, provider, model) -> Iterator[dict]:
        for event, data in sse_events(lines):
            if not data.strip():
                continue
            try:
                obj = json.loads(data)
            except Exception:
                continue
            if isinstance(obj, dict) and obj.get("error"):
                err = obj["error"]
                raise UpstreamError(f"上游流内错误: {err.get('message') or data}", kind="error")
            yield from self._scan_parts(obj)
            for cand in obj.get("candidates") or []:
                if cand.get("finishReason"):
                    yield {"t": "done", "reason": "stop"}
                    return
        yield {"t": "done", "reason": "stop"}


def _gemini_schema(schema: dict) -> dict:
    """Gemini 不接受 additionalProperties/$schema 等字段，做一次浅裁剪。"""
    if not isinstance(schema, dict):
        return {"type": "object", "properties": {}}
    out: dict = {}
    t = schema.get("type")
    if isinstance(t, str):
        out["type"] = t.upper() if t in ("object", "array", "string", "number", "integer",
                                         "boolean", "null") else "STRING"
    if "properties" in schema and isinstance(schema["properties"], dict):
        out["properties"] = {k: _gemini_schema(v) for k, v in schema["properties"].items()}
    if "required" in schema and isinstance(schema["required"], list):
        out["required"] = [str(x) for x in schema["required"]]
    if "items" in schema:
        out["items"] = _gemini_schema(schema["items"])
    if "enum" in schema:
        out["enum"] = schema["enum"]
    if "description" in schema:
        out["description"] = schema["description"]
    return out or {"type": "OBJECT", "properties": {}}


class OllamaAdapter(BaseAdapter):
    type = "ollama"

    def build(self, req: CanonRequest, provider: dict, model: str) -> HttpRequest:
        base = (provider.get("base_url") or "http://127.0.0.1:11434").rstrip("/")
        path = provider.get("path") or "/api/chat"
        options: dict = {"num_predict": req.max_tokens or 2048}
        if req.temperature is not None:
            options["temperature"] = req.temperature
        if req.top_p is not None:
            options["top_p"] = req.top_p
        if req.stop:
            options["stop"] = req.stop
        messages = []
        for m in to_openai_messages(req):
            if m["role"] == "tool":
                messages.append({"role": "tool", "content": m["content"]})
                continue
            content = m.get("content")
            if isinstance(content, list):
                texts = [b.get("text", "") for b in content if b.get("type") == "text"]
                images = [b["image_url"]["url"].split(",", 1)[-1]
                          for b in content if b.get("type") == "image_url"]
                entry: dict = {"role": m["role"], "content": "\n".join(texts)}
                if images:
                    entry["images"] = images
                messages.append(entry)
            else:
                messages.append({"role": m["role"], "content": content or ""})
        body: dict[str, Any] = {"model": model, "messages": messages, "stream": bool(req.stream),
                                "options": options}
        tools = to_openai_tools(req)
        if tools:
            body["tools"] = tools
        return HttpRequest(url=base + path,
                           headers=self._headers(provider),
                           body=json.dumps(body, ensure_ascii=False).encode(),
                           timeout_ms=int(provider.get("timeout_ms") or 120000))

    def parse(self, body: bytes, req, provider, model) -> CanonResponse:
        data = json.loads(body.decode("utf-8", "replace") or "{}")
        if data.get("error"):
            raise UpstreamError(f"ollama 错误: {data['error']}", kind="error")
        msg = data.get("message") or {}
        tool_calls = []
        for tc in msg.get("tool_calls") or []:
            fn = tc.get("function") or {}
            tool_calls.append({"id": new_id("call"), "name": fn.get("name", ""),
                               "arguments": fn.get("arguments") if isinstance(fn.get("arguments"), str)
                               else json.dumps(fn.get("arguments") or {}, ensure_ascii=False)})
        return CanonResponse(id=new_id("ollama"), model=str(data.get("model") or model),
                             text=msg.get("content") or "", tool_calls=tool_calls,
                             finish_reason="stop",
                             prompt_tokens=int(data.get("prompt_eval_count") or 0),
                             completion_tokens=int(data.get("eval_count") or 0), raw=data)

    def parse_stream(self, lines, req, provider, model) -> Iterator[dict]:
        for obj in json_lines(lines):
            if obj.get("error"):
                raise UpstreamError(f"ollama 错误: {obj['error']}", kind="error")
            msg = obj.get("message") or {}
            if msg.get("content"):
                yield {"t": "text", "text": msg["content"]}
            for tc in msg.get("tool_calls") or []:
                fn = tc.get("function") or {}
                yield {"t": "tool_start", "index": 0, "id": new_id("call"),
                       "name": fn.get("name", "")}
                yield {"t": "tool_args", "index": 0,
                       "args": fn.get("arguments") if isinstance(fn.get("arguments"), str)
                       else json.dumps(fn.get("arguments") or {}, ensure_ascii=False)}
            if obj.get("done"):
                yield {"t": "usage", "prompt": int(obj.get("prompt_eval_count") or 0),
                       "completion": int(obj.get("eval_count") or 0)}
                yield {"t": "done", "reason": "stop"}
                return
        yield {"t": "done", "reason": "stop"}


class MockAdapter(BaseAdapter):
    """内置模拟 provider：不需要网络与 key，用于自测与路由演示。

    配置示例：
      {"id":"mock","type":"mock","models":["mock-echo"],"mock":{"reply":"...","latency_ms":20,
       "fail_times":0,"echo":true}}
    """

    type = "mock"

    def build(self, req: CanonRequest, provider: dict, model: str) -> HttpRequest:
        cfg = provider.get("mock") or {}
        payload = {
            "model": model,
            "stream": bool(req.stream),
            "last_user": req.last_user_text()[:2000],
            "prompt_chars": len(req.all_text()),
            "tools": [t["name"] for t in req.tools],
        }
        return HttpRequest(method="MOCK", url="mock://local", headers={},
                           body=json.dumps(payload, ensure_ascii=False).encode(),
                           timeout_ms=int(cfg.get("latency_ms") or 0))

    def parse(self, body: bytes, req, provider, model) -> CanonResponse:
        cfg = provider.get("mock") or {}
        time.sleep(min(float(cfg.get("latency_ms") or 0), 5000) / 1000.0)
        reply = cfg.get("reply") or "（mock provider：未配置 reply，这是固定回复）"
        if cfg.get("echo", True):
            reply = f"{reply}\n\n你刚才说：{req.last_user_text()[:200]}"
        if req.tools and cfg.get("fake_tool"):
            return CanonResponse(id=new_id("mock"), model=model, text="",
                                 tool_calls=[{"id": new_id("call"), "name": req.tools[0]["name"],
                                              "arguments": "{}"}],
                                 finish_reason="tool_calls",
                                 prompt_tokens=req.approx_prompt_tokens,
                                 completion_tokens=len(reply) // 2)
        return CanonResponse(id=new_id("mock"), model=model, text=reply, finish_reason="stop",
                             prompt_tokens=req.approx_prompt_tokens,
                             completion_tokens=max(1, len(reply) // 2))

    def parse_stream(self, lines, req, provider, model) -> Iterator[dict]:
        cfg = provider.get("mock") or {}
        reply = cfg.get("reply") or "（mock provider 流式回复）"
        if cfg.get("echo", True):
            reply = f"{reply} 你刚才说：{req.last_user_text()[:100]}"
        if req.tools and cfg.get("fake_tool"):
            yield {"t": "tool_start", "index": 0, "id": new_id("call"),
                   "name": req.tools[0]["name"]}
            yield {"t": "tool_args", "index": 0, "args": "{}"}
            yield {"t": "done", "reason": "tool_calls"}
            return
        delay = min(float(cfg.get("latency_ms") or 0), 200) / 1000.0
        for i in range(0, len(reply), 8):
            if delay:
                time.sleep(delay / 4)
            yield {"t": "text", "text": reply[i:i + 8]}
        yield {"t": "usage", "prompt": req.approx_prompt_tokens,
               "completion": max(1, len(reply) // 2)}
        yield {"t": "done", "reason": "stop"}


ADAPTERS: dict[str, BaseAdapter] = {
    "openai": OpenAIAdapter(),
    "openai_compatible": OpenAIAdapter(),
    "custom": OpenAIAdapter(),
    "anthropic": AnthropicAdapter(),
    "claude": AnthropicAdapter(),
    "gemini": GeminiAdapter(),
    "google": GeminiAdapter(),
    "ollama": OllamaAdapter(),
    "mock": MockAdapter(),
}


def get_adapter(ptype: str) -> BaseAdapter:
    a = ADAPTERS.get((ptype or "openai").lower())
    if a is None:
        raise ConfigError(f"未知 provider 类型: {ptype}")
    return a


def adapter_types() -> list[str]:
    return sorted({v.type for v in ADAPTERS.values()})
