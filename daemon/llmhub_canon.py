#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""llmhub_canon —— 协议规范化层。

把来自不同「入站协议」（OpenAI /chat/completions、Anthropic /v1/messages、
OpenAI Responses /v1/responses）的请求统一成 CanonRequest，把上游返回统一成
CanonResponse / 规范流事件；再反向序列化回入站协议。

这样协议转换只写 2N 段代码：N 个入站解析 + N 个入站序列化，上游适配器只认规范
结构。参考 tingly-box 的双向协议翻译思路。
"""
from __future__ import annotations

import base64
import json
import time
import uuid
from dataclasses import dataclass, field
from typing import Any


# ── 规范数据结构 ────────────────────────────────────────────────────────────


@dataclass
class Part:
    """消息内容片段。type: text | image | tool_use | tool_result | thinking"""

    type: str = "text"
    text: str = ""
    # image
    image_url: str = ""          # OpenAI 风格（http(s) 或 data: URI）
    media_type: str = ""         # Anthropic 风格，如 image/png
    data: str = ""               # base64（不含 data: 前缀）
    # tool_use
    tool_use_id: str = ""
    name: str = ""
    input: Any = None
    # tool_result
    content: Any = None


@dataclass
class Msg:
    role: str = "user"                     # system | user | assistant | tool
    parts: list[Part] = field(default_factory=list)
    tool_call_id: str = ""
    name: str = ""

    def text(self) -> str:
        return "\n".join(p.text for p in self.parts if p.type == "text" and p.text)


@dataclass
class CanonRequest:
    protocol: str = "openai"               # openai | anthropic | responses
    model: str = ""
    messages: list[Msg] = field(default_factory=list)
    system: str = ""
    tools: list[dict] = field(default_factory=list)
    tool_choice: Any = None
    max_tokens: int = 1024
    temperature: float | None = None
    top_p: float | None = None
    stop: list[str] = field(default_factory=list)
    stream: bool = False
    metadata: dict = field(default_factory=dict)
    raw: dict = field(default_factory=dict)
    # 路由提示：来自 URL（场景）与请求头（X-LLMHub-*）
    hint_provider: str = ""
    hint_scenario: str = ""
    hint_tags: list[str] = field(default_factory=list)
    path: str = ""

    # ── 派生信息（供规则匹配用） ──
    def all_text(self) -> str:
        out = [self.system]
        for m in self.messages:
            out.append(m.text())
        return "\n".join(t for t in out if t)

    def last_user_text(self) -> str:
        for m in reversed(self.messages):
            if m.role == "user":
                return m.text()
        return ""

    @property
    def has_tools(self) -> bool:
        return bool(self.tools) or any(
            p.type == "tool_result" for m in self.messages for p in m.parts
        )

    @property
    def has_image(self) -> bool:
        return any(p.type == "image" for m in self.messages for p in m.parts)

    @property
    def approx_prompt_tokens(self) -> int:
        """粗估：4 字符 ≈ 1 token，中文按 1.6 字符/token 折中取 2。"""
        chars = len(self.all_text())
        extra = len(json.dumps(self.tools, ensure_ascii=False)) if self.tools else 0
        return int(chars / 2.2) + int(extra / 4) + 8


@dataclass
class CanonResponse:
    id: str = ""
    model: str = ""
    text: str = ""
    tool_calls: list[dict] = field(default_factory=list)
    finish_reason: str = "stop"
    prompt_tokens: int = 0
    completion_tokens: int = 0
    raw: dict = field(default_factory=dict)


def new_id(prefix: str = "chatcmpl") -> str:
    return f"{prefix}-{uuid.uuid4().hex[:24]}"


def now_ts() -> int:
    return int(time.time())


# ── 工具 schema 归一 ────────────────────────────────────────────────────────


def _tool_from_any(t: dict) -> dict | None:
    """把三种协议的工具定义归一成 {name, description, parameters}。"""
    if not isinstance(t, dict):
        return None
    if t.get("type") == "function" and isinstance(t.get("function"), dict):
        f = t["function"]
        return {
            "name": f.get("name", ""),
            "description": f.get("description", ""),
            "parameters": f.get("parameters") or {"type": "object", "properties": {}},
        }
    if "input_schema" in t:  # Anthropic
        return {
            "name": t.get("name", ""),
            "description": t.get("description", ""),
            "parameters": t.get("input_schema") or {"type": "object", "properties": {}},
        }
    if "parameters" in t or "name" in t:  # 已是归一形态 / Gemini 扁平形
        return {
            "name": t.get("name", ""),
            "description": t.get("description", ""),
            "parameters": t.get("parameters") or {"type": "object", "properties": {}},
        }
    return None


def normalize_tool_choice(tc: Any) -> dict | None:
    """把各协议的 tool_choice 归一成内部形式 {"type": auto|any|none|tool, "name": str}。"""
    if tc is None:
        return None
    if isinstance(tc, str):
        low = tc.strip().lower()
        if low == "auto":
            return {"type": "auto"}
        if low in ("required", "any"):
            return {"type": "any"}
        if low == "none":
            return {"type": "none"}
        return None
    if isinstance(tc, dict):
        t = str(tc.get("type") or "").strip().lower()
        if t in ("auto", "any", "none"):
            return {"type": t}
        if t == "required":
            return {"type": "any"}
        name = (tc.get("name")
                or (tc.get("function") or {}).get("name")
                or (tc.get("tool") or {}).get("name"))
        if t in ("tool", "function", "function_call") and name:
            return {"type": "tool", "name": str(name)}
        if t == "allowed_tools":                      # Responses 协议
            tools = tc.get("tools") or []
            first = tools[0] if tools and isinstance(tools[0], dict) else None
            if first and first.get("name"):
                return {"type": "tool", "name": str(first["name"])}
            return {"type": "any"}
    return None


def openai_tool_choice(tc: Any) -> Any:
    """内部形式 → OpenAI 形式（字符串或 function 对象）。"""
    n = normalize_tool_choice(tc)
    if not n:
        return None
    if n["type"] == "tool":
        return {"type": "function", "function": {"name": n.get("name", "")}}
    if n["type"] == "any":
        return "required"
    return n["type"]


def anthropic_tool_choice(tc: Any) -> dict | None:
    """内部形式 → Anthropic 形式（{type: auto|any|none|tool}）。"""
    n = normalize_tool_choice(tc)
    if not n:
        return None
    if n["type"] == "tool":
        return {"type": "tool", "name": n.get("name", "")}
    return {"type": n["type"]}


def gemini_tool_choice(tc: Any) -> dict | None:
    """内部形式 → Gemini toolConfig 片段。"""
    n = normalize_tool_choice(tc)
    if not n:
        return None
    mode = {"auto": "AUTO", "any": "ANY", "none": "NONE", "tool": "ANY"}.get(n["type"])
    if not mode:
        return None
    cfg: dict[str, Any] = {"mode": mode}
    if n["type"] == "tool" and n.get("name"):
        cfg["allowedFunctionNames"] = [n["name"]]
    return {"functionCallingConfig": cfg}


# ── 入站解析：OpenAI /v1/chat/completions ───────────────────────────────────


def _openai_content_to_parts(content: Any) -> list[Part]:
    if content is None:
        return []
    if isinstance(content, str):
        return [Part(type="text", text=content)]
    parts: list[Part] = []
    for block in content if isinstance(content, list) else []:
        if not isinstance(block, dict):
            if isinstance(block, str):
                parts.append(Part(type="text", text=block))
            continue
        btype = block.get("type")
        if btype in ("text", "input_text", "output_text"):
            parts.append(Part(type="text", text=block.get("text", "")))
        elif btype in ("image_url", "input_image"):
            url = block.get("image_url")
            if isinstance(url, dict):
                url = url.get("url", "")
            parts.append(Part(type="image", image_url=url or ""))
        elif btype == "tool_result" or btype == "function_call_output":
            parts.append(
                Part(
                    type="tool_result",
                    tool_use_id=block.get("tool_use_id") or block.get("call_id") or "",
                    content=block.get("content"),
                )
            )
    return parts


def parse_openai_chat(body: dict) -> CanonRequest:
    req = CanonRequest(protocol="openai", raw=body)
    req.model = str(body.get("model") or "")
    req.stream = bool(body.get("stream"))
    if body.get("max_tokens") is not None:
        req.max_tokens = int(body.get("max_tokens") or 0)
    elif body.get("max_completion_tokens") is not None:
        req.max_tokens = int(body.get("max_completion_tokens") or 0)
    if body.get("temperature") is not None:
        req.temperature = float(body["temperature"])
    if body.get("top_p") is not None:
        req.top_p = float(body["top_p"])
    stop = body.get("stop")
    if isinstance(stop, str):
        req.stop = [stop]
    elif isinstance(stop, list):
        req.stop = [str(s) for s in stop]

    for raw_msg in body.get("messages") or []:
        if not isinstance(raw_msg, dict):
            continue
        role = str(raw_msg.get("role") or "user")
        msg = Msg(role=role)
        if role == "tool" or role == "function":
            msg.tool_call_id = str(raw_msg.get("tool_call_id") or raw_msg.get("name") or "")
            msg.parts = [Part(type="tool_result", tool_use_id=msg.tool_call_id,
                              content=raw_msg.get("content"))]
        else:
            msg.parts = _openai_content_to_parts(raw_msg.get("content"))
        for tc in raw_msg.get("tool_calls") or []:
            fn = (tc or {}).get("function") or {}
            args = fn.get("arguments")
            if isinstance(args, str):
                try:
                    args = json.loads(args) if args.strip() else {}
                except Exception:
                    args = {"_raw": args}
            msg.parts.append(
                Part(type="tool_use", tool_use_id=tc.get("id", ""), name=fn.get("name", ""),
                     input=args or {})
            )
        if msg.role == "system":
            req.system = (req.system + "\n" + msg.text()).strip()
        req.messages.append(msg)

    req.tools = [t for t in (_tool_from_any(x) for x in (body.get("tools") or [])) if t]
    req.tool_choice = normalize_tool_choice(body.get("tool_choice"))
    return req


# ── 入站解析：Anthropic /v1/messages ────────────────────────────────────────


def _anthropic_content_to_parts(content: Any) -> list[Part]:
    if isinstance(content, str):
        return [Part(type="text", text=content)]
    parts: list[Part] = []
    for block in content if isinstance(content, list) else []:
        if not isinstance(block, dict):
            continue
        btype = block.get("type")
        if btype == "text":
            parts.append(Part(type="text", text=block.get("text", "")))
        elif btype == "image":
            src = block.get("source") or {}
            if src.get("type") == "base64":
                parts.append(Part(type="image", media_type=src.get("media_type", ""),
                                  data=src.get("data", "")))
            elif src.get("type") == "url":
                parts.append(Part(type="image", image_url=src.get("url", "")))
        elif btype == "tool_use":
            parts.append(Part(type="tool_use", tool_use_id=block.get("id", ""),
                              name=block.get("name", ""), input=block.get("input") or {}))
        elif btype == "tool_result":
            parts.append(Part(type="tool_result", tool_use_id=block.get("tool_use_id", ""),
                              content=block.get("content")))
        elif btype == "thinking":
            parts.append(Part(type="thinking", text=block.get("thinking", "")))
    return parts


def parse_anthropic_messages(body: dict) -> CanonRequest:
    req = CanonRequest(protocol="anthropic", raw=body)
    req.model = str(body.get("model") or "")
    req.stream = bool(body.get("stream"))
    req.max_tokens = int(body.get("max_tokens") or 1024)
    if body.get("temperature") is not None:
        req.temperature = float(body["temperature"])
    if body.get("top_p") is not None:
        req.top_p = float(body["top_p"])
    if isinstance(body.get("stop_sequences"), list):
        req.stop = [str(s) for s in body["stop_sequences"]]
    sys = body.get("system")
    if isinstance(sys, str):
        req.system = sys
    elif isinstance(sys, list):
        req.system = "\n".join(
            b.get("text", "") for b in sys if isinstance(b, dict) and b.get("type") == "text"
        )
    for raw_msg in body.get("messages") or []:
        if not isinstance(raw_msg, dict):
            continue
        msg = Msg(role=str(raw_msg.get("role") or "user"))
        msg.parts = _anthropic_content_to_parts(raw_msg.get("content"))
        req.messages.append(msg)
    req.tools = [t for t in (_tool_from_any(x) for x in (body.get("tools") or [])) if t]
    req.tool_choice = normalize_tool_choice(body.get("tool_choice"))
    if req.system:
        req.messages.insert(0, Msg(role="system", parts=[Part(type="text", text=req.system)]))
    return req


# ── 入站解析：OpenAI Responses /v1/responses ────────────────────────────────


def parse_responses(body: dict) -> CanonRequest:
    req = CanonRequest(protocol="responses", raw=body)
    req.model = str(body.get("model") or "")
    req.stream = bool(body.get("stream"))
    if body.get("max_output_tokens") is not None:
        req.max_tokens = int(body.get("max_output_tokens") or 0)
    if body.get("temperature") is not None:
        req.temperature = float(body["temperature"])

    instr = body.get("instructions")
    if isinstance(instr, str) and instr:
        req.system = instr
        req.messages.append(Msg(role="system", parts=[Part(type="text", text=instr)]))

    inp = body.get("input")
    if isinstance(inp, str):
        req.messages.append(Msg(role="user", parts=[Part(type="text", text=inp)]))
    elif isinstance(inp, list):
        for item in inp:
            if not isinstance(item, dict):
                continue
            itype = item.get("type")
            if itype == "message" or "role" in item:
                role = str(item.get("role") or "user")
                req.messages.append(Msg(role=role, parts=_openai_content_to_parts(item.get("content"))))
            elif itype == "function_call":
                args = item.get("arguments")
                if isinstance(args, str):
                    try:
                        args = json.loads(args) if args.strip() else {}
                    except Exception:
                        args = {"_raw": args}
                req.messages.append(
                    Msg(role="assistant", parts=[
                        Part(type="tool_use", tool_use_id=item.get("call_id", ""),
                             name=item.get("name", ""), input=args or {})
                    ])
                )
            elif itype == "function_call_output":
                req.messages.append(
                    Msg(role="tool", tool_call_id=str(item.get("call_id") or ""),
                        parts=[Part(type="tool_result", tool_use_id=item.get("call_id", ""),
                                    content=item.get("output"))])
                )
    req.tools = [t for t in (_tool_from_any(x) for x in (body.get("tools") or [])) if t]
    req.tool_choice = normalize_tool_choice(body.get("tool_choice"))
    return req


PARSERS = {
    "openai": parse_openai_chat,
    "anthropic": parse_anthropic_messages,
    "responses": parse_responses,
}


# ── 出站序列化：规范响应 → 入站协议（非流式） ──────────────────────────────


def to_openai_response(resp: CanonResponse, req: CanonRequest) -> dict:
    message: dict = {"role": "assistant", "content": resp.text or None}
    if resp.tool_calls:
        message["tool_calls"] = [
            {
                "id": tc.get("id") or new_id("call"),
                "type": "function",
                "function": {
                    "name": tc.get("name", ""),
                    "arguments": tc.get("arguments")
                    if isinstance(tc.get("arguments"), str)
                    else json.dumps(tc.get("arguments") or {}, ensure_ascii=False),
                },
            }
            for tc in resp.tool_calls
        ]
        if not resp.text:
            message["content"] = None
    return {
        "id": resp.id or new_id(),
        "object": "chat.completion",
        "created": now_ts(),
        "model": resp.model or req.model,
        "choices": [
            {
                "index": 0,
                "message": message,
                "finish_reason": _openai_finish(resp.finish_reason, resp),
                "logprobs": None,
            }
        ],
        "usage": {
            "prompt_tokens": resp.prompt_tokens or req.approx_prompt_tokens,
            "completion_tokens": resp.completion_tokens,
            "total_tokens": (resp.prompt_tokens or req.approx_prompt_tokens) + resp.completion_tokens,
        },
        "system_fingerprint": "llmhub",
    }


def _openai_finish(reason: str, resp: CanonResponse) -> str:
    if resp.tool_calls:
        return "tool_calls"
    if reason in ("length", "max_tokens"):
        return "length"
    if reason in ("tool_use", "tool_calls"):
        return "tool_calls"
    if reason in ("stop", "end_turn", "stop_sequence", ""):
        return "stop"
    return reason


def to_anthropic_response(resp: CanonResponse, req: CanonRequest) -> dict:
    content: list[dict] = []
    if resp.text:
        content.append({"type": "text", "text": resp.text})
    for tc in resp.tool_calls:
        args = tc.get("arguments")
        if isinstance(args, str):
            try:
                args = json.loads(args) if args.strip() else {}
            except Exception:
                args = {"_raw": args}
        content.append({
            "type": "tool_use",
            "id": tc.get("id") or new_id("toolu"),
            "name": tc.get("name", ""),
            "input": args or {},
        })
    if not content:
        content.append({"type": "text", "text": ""})
    stop_reason = {"length": "max_tokens", "max_tokens": "max_tokens",
                   "tool_calls": "tool_use", "tool_use": "tool_use"}.get(resp.finish_reason, "end_turn")
    return {
        "id": resp.id or new_id("msg"),
        "type": "message",
        "role": "assistant",
        "model": resp.model or req.model,
        "content": content,
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "usage": {
            "input_tokens": resp.prompt_tokens or req.approx_prompt_tokens,
            "output_tokens": resp.completion_tokens,
        },
    }


def to_responses_response(resp: CanonResponse, req: CanonRequest) -> dict:
    rid = resp.id or new_id("resp")
    out: list[dict] = []
    if resp.text:
        out.append({
            "type": "message",
            "id": new_id("msg"),
            "status": "completed",
            "role": "assistant",
            "content": [{"type": "output_text", "text": resp.text, "annotations": []}],
        })
    for tc in resp.tool_calls:
        args = tc.get("arguments")
        out.append({
            "type": "function_call",
            "id": new_id("fc"),
            "call_id": tc.get("id") or new_id("call"),
            "name": tc.get("name", ""),
            "arguments": args if isinstance(args, str) else json.dumps(args or {}, ensure_ascii=False),
            "status": "completed",
        })
    return {
        "id": rid,
        "object": "response",
        "created_at": now_ts(),
        "status": "completed",
        "model": resp.model or req.model,
        "output": out,
        "output_text": resp.text,
        "usage": {
            "input_tokens": resp.prompt_tokens or req.approx_prompt_tokens,
            "output_tokens": resp.completion_tokens,
            "total_tokens": (resp.prompt_tokens or req.approx_prompt_tokens) + resp.completion_tokens,
        },
        "parallel_tool_calls": True,
        "tool_choice": "auto",
        "tools": [],
    }


SERIALIZERS = {
    "openai": to_openai_response,
    "anthropic": to_anthropic_response,
    "responses": to_responses_response,
}


def serialize_response(resp: CanonResponse, req: CanonRequest) -> dict:
    return SERIALIZERS.get(req.protocol, to_openai_response)(resp, req)


# ── 出站序列化：规范流事件 → 入站协议 SSE ──────────────────────────────────
#
# 规范事件（上游适配器产出）：
#   {"t":"text","text":str}
#   {"t":"tool_start","index":i,"id":..,"name":..}
#   {"t":"tool_args","index":i,"args":str}
#   {"t":"reasoning","text":str}
#   {"t":"usage","prompt":n,"completion":m}
#   {"t":"done","reason":"stop"}


class StreamWriter:
    """把规范事件翻成入站协议的 SSE 文本。"""

    def __init__(self, req: CanonRequest, model: str):
        self.req = req
        self.protocol = req.protocol
        self.model = model
        self.id = new_id("msg" if req.protocol == "anthropic" else
                         ("resp" if req.protocol == "responses" else "chatcmpl"))
        self.sent_role = False
        self.block_open = False          # anthropic 文本块是否已开
        self.block_index = 0
        self.tool_buf: dict[int, dict] = {}
        self.prompt_tokens = req.approx_prompt_tokens
        self.completion_tokens = 0
        self.finish_reason = "stop"

    # -- 辅助 --
    @staticmethod
    def sse(payload: dict, event: str | None = None) -> bytes:
        head = f"event: {event}\n" if event else ""
        return (head + "data: " + json.dumps(payload, ensure_ascii=False) + "\n\n").encode()

    def _usage(self) -> dict:
        return self.req.input_tokens if hasattr(self.req, "input_tokens") else {}

    # -- 主入口 --
    def feed(self, ev: dict) -> list[bytes]:
        t = ev.get("t")
        if t == "usage":
            self.prompt_tokens = ev.get("prompt") or self.prompt_tokens
            self.completion_tokens = ev.get("completion") or self.completion_tokens
            return []
        if t == "done":
            self.finish_reason = ev.get("reason") or "stop"
            return self._finalize()
        if t == "text":
            return self._text(ev.get("text", ""))
        if t == "tool_start":
            return self._tool_start(ev)
        if t == "tool_args":
            return self._tool_args(ev)
        return []

    # -- openai --
    def _openai_chunk(self, delta: dict, finish: str | None = None) -> bytes:
        return self.sse({
            "id": self.id,
            "object": "chat.completion.chunk",
            "created": now_ts(),
            "model": self.model,
            "choices": [{"index": 0, "delta": delta, "finish_reason": finish, "logprobs": None}],
        })

    # -- anthropic --
    def _text(self, text: str) -> list[bytes]:
        if not text:
            return []
        if self.protocol == "openai":
            out = []
            if not self.sent_role:
                self.sent_role = True
                out.append(self._openai_chunk({"role": "assistant", "content": ""}))
            out.append(self._openai_chunk({"content": text}))
            return out
        if self.protocol == "anthropic":
            out = []
            if not self.sent_role:
                out.append(self._anthropic_start())
            if not self.block_open:
                self.block_open = True
                out.append(self.sse({"type": "content_block_start", "index": self.block_index,
                                     "content_block": {"type": "text", "text": ""}},
                                    event="content_block_start"))
            out.append(self.sse({"type": "content_block_delta", "index": self.block_index,
                                 "delta": {"type": "text_delta", "text": text}},
                                event="content_block_delta"))
            return out
        # responses
        out = []
        if not self.sent_role:
            self.sent_role = True
            out.append(self.sse({"type": "response.created", "response": {
                "id": self.id, "object": "response", "status": "in_progress",
                "model": self.model, "output": []}}))
            out.append(self.sse({"type": "response.output_item.added", "output_index": 0, "item": {
                "type": "message", "id": new_id("msg"), "status": "in_progress",
                "role": "assistant", "content": []}}))
            out.append(self.sse({"type": "response.content_part.added", "item_id": new_id("msg"),
                                 "output_index": 0, "content_index": 0,
                                 "part": {"type": "output_text", "text": "", "annotations": []}}))
        out.append(self.sse({"type": "response.output_text.delta", "item_id": self.id,
                             "output_index": 0, "content_index": 0, "delta": text}))
        return out

    def _anthropic_start(self) -> bytes:
        return self.sse({
            "type": "message_start",
            "message": {
                "id": self.id, "type": "message", "role": "assistant",
                "model": self.model, "content": [],
                "stop_reason": None, "stop_sequence": None,
                "usage": {"input_tokens": self.prompt_tokens, "output_tokens": 0},
            },
        }, event="message_start")

    def _tool_start(self, ev: dict) -> list[bytes]:
        index = int(ev.get("index") or 0)
        self.tool_buf[index] = {"id": ev.get("id") or new_id("call"), "name": ev.get("name", ""),
                                "args": ""}
        if self.protocol == "openai":
            out = []
            if not self.sent_role:
                self.sent_role = True
                out.append(self._openai_chunk({"role": "assistant", "content": ""}))
            out.append(self._openai_chunk({"tool_calls": [{
                "index": index, "id": self.tool_buf[index]["id"], "type": "function",
                "function": {"name": self.tool_buf[index]["name"], "arguments": ""}}]}))
            return out
        if self.protocol == "anthropic":
            out = []
            if not self.sent_role:
                out.append(self._anthropic_start())
            if self.block_open:
                out.append(self.sse({"type": "content_block_stop", "index": self.block_index},
                                    event="content_block_stop"))
                self.block_open = False
                self.block_index += 1
            out.append(self.sse({"type": "content_block_start", "index": self.block_index,
                                 "content_block": {"type": "tool_use",
                                                   "id": self.tool_buf[index]["id"],
                                                   "name": self.tool_buf[index]["name"],
                                                   "input": {}}},
                                event="content_block_start"))
            self.tool_buf[index]["block_index"] = self.block_index
            return out
        return []

    def _tool_args(self, ev: dict) -> list[bytes]:
        index = int(ev.get("index") or 0)
        args = ev.get("args", "")
        if index not in self.tool_buf:
            self._tool_start({"index": index})
        self.tool_buf[index]["args"] += args
        if self.protocol == "openai":
            return [self._openai_chunk({"tool_calls": [{
                "index": index, "function": {"arguments": args}}]})]
        if self.protocol == "anthropic":
            return [self.sse({"type": "content_block_delta",
                              "index": self.tool_buf[index].get("block_index", self.block_index),
                              "delta": {"type": "input_json_delta", "partial_json": args}},
                             event="content_block_delta")]
        return []

    def _finalize(self) -> list[bytes]:
        out: list[bytes] = []
        if self.protocol == "openai":
            if not self.sent_role:
                out.append(self._openai_chunk({"role": "assistant", "content": ""}))
            out.append(self._openai_chunk(
                {}, finish=_openai_finish(self.finish_reason, CanonResponse(tool_calls=[
                    {"id": k} for k in self.tool_buf]))))
            out.append(b"data: [DONE]\n\n")
            return out
        if self.protocol == "anthropic":
            if not self.sent_role:
                out.append(self._anthropic_start())
            if self.block_open:
                out.append(self.sse({"type": "content_block_stop", "index": self.block_index},
                                    event="content_block_stop"))
            for idx, buf in self.tool_buf.items():
                out.append(self.sse({"type": "content_block_stop",
                                     "index": buf.get("block_index", self.block_index)},
                                    event="content_block_stop"))
            stop_reason = ("tool_use" if self.tool_buf else
                           {"length": "max_tokens", "max_tokens": "max_tokens"}.get(
                               self.finish_reason, "end_turn"))
            out.append(self.sse({"type": "message_delta",
                                 "delta": {"stop_reason": stop_reason, "stop_sequence": None},
                                 "usage": {"output_tokens": self.completion_tokens}},
                                event="message_delta"))
            out.append(self.sse({"type": "message_stop"}, event="message_stop"))
            return out
        # responses
        if not self.sent_role:
            out.extend(self._text(""))
        out.append(self.sse({"type": "response.output_text.done", "item_id": self.id,
                             "output_index": 0, "content_index": 0, "text": ""}))
        for i, buf in enumerate(self.tool_buf.values()):
            out.append(self.sse({"type": "response.output_item.added", "output_index": i + 1,
                                 "item": {"type": "function_call", "id": new_id("fc"),
                                          "call_id": buf["id"], "name": buf["name"],
                                          "arguments": "", "status": "in_progress"}}))
            out.append(self.sse({"type": "response.function_call_arguments.delta",
                                 "item_id": buf["id"], "output_index": i + 1, "delta": buf["args"]}))
            out.append(self.sse({"type": "response.function_call_arguments.done",
                                 "item_id": buf["id"], "output_index": i + 1,
                                 "arguments": buf["args"]}))
        out.append(self.sse({"type": "response.completed", "response": {
            "id": self.id, "object": "response", "status": "completed", "model": self.model,
            "output": [], "usage": {"input_tokens": self.prompt_tokens,
                                    "output_tokens": self.completion_tokens,
                                    "total_tokens": self.prompt_tokens + self.completion_tokens}}}))
        return out
