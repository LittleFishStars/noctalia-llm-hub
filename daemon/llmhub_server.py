#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""llmhub_server —— asyncio HTTP/1.1 服务端：入站协议解析、路由、协议翻译、SSE 流式。

只依赖标准库。所有上游调用都在线程池里同步执行，流式事件经 asyncio.Queue 泵到
chunked 响应；客户端断开时通过 threading.Event 反向打断上游读取。
"""
from __future__ import annotations

import asyncio
import hmac
import json
import os
import re
import signal
import socket
import threading
import time
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

from llmhub_adapters import (ConfigError, HttpRequest, UpstreamError, adapter_types, check_status,
                             get_adapter, http_request)
from llmhub_canon import (PARSERS, CanonRequest, CanonResponse, Msg, Part, StreamWriter,
                          new_id, serialize_response)
from llmhub_core import (VERSION, ConfigStore, Executor, RouteDecision, Router, Stats,
                         model_catalog)

MAX_BODY = 64 * 1024 * 1024
STREAM_IDLE_TIMEOUT = 600.0

STATUS_FOR_KIND = {
    "auth": 401,
    "rate_limit": 429,
    "timeout": 504,
    "connect": 502,
    "bad_request": 400,
    "error": 502,
}


class AbortSignal(Exception):
    """客户端断开，通知上游读取线程停止。"""


def error_payload(protocol: str, message: str, kind: str = "error", status: int = 502) -> dict:
    if protocol == "anthropic":
        return {"type": "error", "error": {"type": "api_error" if status >= 500 else "invalid_request_error",
                                           "message": message}}
    if protocol == "responses":
        return {"error": {"message": message, "type": kind, "code": kind, "param": None}}
    return {"error": {"message": message, "type": "llmhub_error", "code": kind, "param": None}}


def status_for(e: Exception) -> int:
    return STATUS_FOR_KIND.get(getattr(e, "kind", "error"), 502)


_SECRET_QS = ("key", "api_key", "apikey", "token", "access_token", "auth", "password")


def mask_url(url: Any) -> Any:
    """打码 URL 里可能携带密钥的查询参数（如 Gemini 的 ?key=）。"""
    if not url:
        return url
    s = str(url)
    for name in _SECRET_QS:
        s = re.sub(rf"([?&]{name}=)[^&\s]*", r"\1***", s, flags=re.I)
    return s


def mask_server(srv: Any) -> Any:
    """server 配置段的对外视图：api_key 只暴露"是否已设置"。"""
    if not isinstance(srv, dict):
        return srv
    out = {k: v for k, v in srv.items() if k != "api_key"}
    out["has_api_key"] = bool(srv.get("api_key"))
    if "api_key" in srv and srv.get("api_key"):
        out["api_key_masked"] = "***"
    return out


class Gateway:
    def __init__(self, store: ConfigStore, stats: Stats, executor: Executor, router: Router,
                 quiet: bool = False):
        self.store = store
        self.stats = stats
        self.executor = executor
        self.router = router
        self.quiet = quiet
        self.started_at = time.time()
        self.port = 0
        self.host = ""
        self._in_flight = 0
        self._lock = threading.RLock()

    # ── 日志 ────────────────────────────────────────────────────────────────
    def log(self, msg: str):
        if not self.quiet:
            print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)

    # ── 主循环 ──────────────────────────────────────────────────────────────
    async def serve(self, host: str, port: int):
        self.host, self.port = host, port
        server = await asyncio.start_server(self._on_client, host, port, limit=MAX_BODY,
                                            reuse_address=True)
        addrs = ", ".join(str(s.getsockname()) for s in server.sockets or [])
        self.log(f"llm-hub 网关已启动 v{VERSION} → http://{host}:{port}  ({addrs})")
        loop = asyncio.get_running_loop()
        stop = asyncio.Event()

        def _sig(*_a):
            stop.set()

        for s in (signal.SIGTERM, signal.SIGINT):
            try:
                loop.add_signal_handler(s, _sig)
            except (NotImplementedError, RuntimeError):
                pass
        async with server:
            await stop.wait()
        self.log("正在关闭…")
        self.stats.flush(force=True)

    # ── 连接处理 ────────────────────────────────────────────────────────────
    async def _on_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        self._in_flight += 1
        try:
            while True:
                try:
                    req = await asyncio.wait_for(self._read_request(reader), timeout=STREAM_IDLE_TIMEOUT)
                except (asyncio.IncompleteReadError, ConnectionResetError, asyncio.TimeoutError,
                        ValueError):
                    break
                if req is None:
                    break
                keep = await self._handle(req, writer)
                if not keep:
                    break
        except Exception as e:  # noqa: BLE001
            self.log(f"连接异常: {type(e).__name__}: {e}")
        finally:
            self._in_flight -= 1
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass

    async def _read_request(self, reader: asyncio.StreamReader) -> dict | None:
        try:
            head = await reader.readuntil(b"\r\n\r\n")
        except (asyncio.IncompleteReadError, asyncio.LimitOverrunError):
            return None
        text = head.decode("utf-8", "replace")
        lines = text.split("\r\n")
        parts = lines[0].split()
        if len(parts) < 2:
            return None
        method, target = parts[0].upper(), parts[1]
        if "%" in target:      # 非 ASCII 路径（如 /s/背景任务/...）先解码再路由
            target = unquote(target)
        headers: dict[str, str] = {}
        for line in lines[1:]:
            if not line or ":" not in line:
                continue
            k, _, v = line.partition(":")
            headers[k.strip().lower()] = v.strip()
        body = b""
        if headers.get("transfer-encoding", "").lower() == "chunked":
            while True:
                size_line = (await reader.readline()).strip()
                try:
                    size = int(size_line.split(b";")[0] or b"0", 16)
                except ValueError:
                    break
                if size == 0:
                    await reader.readline()
                    break
                body += await reader.readexactly(size)
                await reader.readexactly(2)
        elif "content-length" in headers:
            n = int(headers["content-length"])
            if n > MAX_BODY:
                raise ValueError("请求体过大")
            body = await reader.readexactly(n)
        return {"method": method, "target": target, "headers": headers, "body": body}

    # ── 响应工具 ────────────────────────────────────────────────────────────
    @staticmethod
    async def _write_response(writer, status: int, body: bytes, ctype: str = "application/json",
                              extra: dict | None = None, keep_alive: bool = True):
        reason = {200: "OK", 400: "Bad Request", 401: "Unauthorized", 403: "Forbidden",
                  404: "Not Found", 405: "Method Not Allowed", 413: "Payload Too Large",
                  429: "Too Many Requests", 500: "Internal Server Error",
                  502: "Bad Gateway", 504: "Gateway Timeout"}.get(status, "OK")
        head = [f"HTTP/1.1 {status} {reason}",
                f"Content-Type: {ctype}",
                f"Content-Length: {len(body)}",
                "Access-Control-Allow-Origin: *",
                "Access-Control-Allow-Headers: *",
                "Server: llm-hub/" + VERSION,
                ("Connection: keep-alive" if keep_alive else "Connection: close")]
        for k, v in (extra or {}).items():
            head.append(f"{k}: {v}")
        writer.write(("\r\n".join(head) + "\r\n\r\n").encode() + body)
        await writer.drain()

    @staticmethod
    async def _start_stream(writer, extra: dict | None = None):
        head = ["HTTP/1.1 200 OK",
                "Content-Type: text/event-stream; charset=utf-8",
                "Cache-Control: no-cache, no-transform",
                "X-Accel-Buffering: no",
                "Transfer-Encoding: chunked",
                "Access-Control-Allow-Origin: *",
                "Server: llm-hub/" + VERSION,
                "Connection: keep-alive"]
        for k, v in (extra or {}).items():
            head.append(f"{k}: {v}")
        writer.write(("\r\n".join(head) + "\r\n\r\n").encode())
        await writer.drain()

    @staticmethod
    async def _write_chunk(writer, data: bytes):
        if not data:
            return
        writer.write(f"{len(data):X}\r\n".encode() + data + b"\r\n")
        await writer.drain()

    @staticmethod
    async def _end_stream(writer):
        try:
            writer.write(b"0\r\n\r\n")
            await writer.drain()
        except Exception:
            pass

    # ── 请求分发 ────────────────────────────────────────────────────────────
    async def _handle(self, req: dict, writer) -> bool:
        store = self.store
        store.maybe_reload()
        cfg = store.snapshot()
        method, target = req["method"], req["target"]
        u = urlsplit(target)
        path = u.path
        query = parse_qs(u.query or "")
        headers = req["headers"]
        protocol = "openai"
        hm = {"Access-Control-Allow-Origin": "*", "Access-Control-Allow-Headers": "*"}

        if method == "OPTIONS":
            await self._write_response(writer, 200, b"", "text/plain", hm)
            return True

        # 场景前缀 /s/<scenario>/…
        scenario = ""
        if path.startswith("/s/"):
            _, _, rest = path.partition("/s/")
            scenario, _, tail = rest.partition("/")
            path = "/" + tail

        # 鉴权
        api_key = str((cfg.get("server") or {}).get("api_key") or "")
        if api_key and path not in ("/health",):
            got = ""
            auth = headers.get("authorization", "")
            if auth.lower().startswith("bearer "):
                got = auth[7:].strip()
            got = got or headers.get("x-api-key", "")
            if not hmac.compare_digest(got, api_key):
                body = error_payload(protocol, "无效的本地网关 API Key（server.api_key）",
                                     "auth", 401)
                await self._write_response(writer, 401, json.dumps(body).encode(), "application/json",
                                           hm)
                return True

        try:
            body_json = json.loads(req["body"].decode("utf-8") or "{}") if req["body"] else {}
        except Exception:
            body_json = {}

        # 管理 / 辅助接口
        if path in ("/", "/index"):
            info = {
                "name": "llm-hub 网关", "version": VERSION, "uptime_s": round(time.time() - self.started_at, 1),
                "endpoints": {
                    "health": "GET /health", "stats": "GET /stats", "logs": "GET /logs?limit=N",
                    "models": "GET /v1/models", "route（干跑）": "POST /route 或 GET /route?model=X",
                    "openai": "POST /v1/chat/completions", "openai_legacy": "POST /v1/completions",
                    "anthropic": "POST /v1/messages", "responses": "POST /v1/responses",
                    "embeddings": "POST /v1/embeddings", "ask": "POST /admin/ask",
                    "config": "GET /config", "test": "POST /admin/test",
                    "toggle": "POST /admin/providers/<id>/toggle", "reload": "POST /admin/reload",
                    "scenario": "任意路径前加 /s/<scenario>",
                },
            }
            await self._write_response(writer, 200, json.dumps(info, ensure_ascii=False).encode(),
                                       "application/json", hm)
            return True
        if path == "/health":
            await self._write_response(writer, 200,
                                       json.dumps(self.health(), ensure_ascii=False).encode(),
                                       "application/json", hm)
            return True
        if path == "/stats":
            await self._write_response(writer, 200,
                                       json.dumps(self.stats.snapshot(), ensure_ascii=False).encode(),
                                       "application/json", hm)
            return True
        if path == "/logs":
            limit = int((query.get("limit") or ["50"])[0] or 50)
            body = {"logs": self.stats.logs_list(limit)}
            await self._write_response(writer, 200, json.dumps(body, ensure_ascii=False).encode(),
                                       "application/json", hm)
            return True
        if path == "/config":
            await self._write_response(writer, 200,
                                       json.dumps(self.config_view(cfg), ensure_ascii=False).encode(),
                                       "application/json", hm)
            return True
        if path == "/admin/reload":
            changed = store.reload(force=True)
            await self._write_response(writer, 200, json.dumps(
                {"ok": True, "reloaded": changed, "errors": store.errors()},
                ensure_ascii=False).encode(), "application/json", hm)
            return True
        if path == "/admin/stats/reset":
            keep = str((body_json or {}).get("keep_cumulative", True)).lower() != "false"
            self.stats.reset(keep_cumulative=keep)
            await self._write_response(writer, 200, b'{"ok":true}', "application/json", hm)
            return True
        if path == "/admin/logs/clear":
            self.stats.clear_logs()
            await self._write_response(writer, 200, b'{"ok":true}', "application/json", hm)
            return True
        if path.startswith("/admin/providers/") and path.endswith("/toggle"):
            pid = path[len("/admin/providers/"):-len("/toggle")]
            enabled = bool((body_json or {}).get("enabled", True))

            def _mut(c):
                for p in c.get("providers") or []:
                    if p.get("id") == pid:
                        p["enabled"] = enabled
                return c

            store.mutate(_mut)
            self.log(f"provider {pid} → {'启用' if enabled else '停用'}")
            await self._write_response(writer, 200, json.dumps({"ok": True, "id": pid,
                                                                "enabled": enabled}).encode(),
                                       "application/json", hm)
            return True
        if path == "/admin/test":
            return await self._admin_test(writer, body_json or {}, hm)
        if path == "/admin/ask":
            return await self._admin_ask(writer, body_json or {}, headers, hm)
        if path == "/v1/models" or path == "/models":
            catalog = model_catalog(cfg, self.stats)
            aliases = [{"id": k, "object": "model", "owned_by": "alias", "providers": [v],
                        "enabled": True} for k, v in (cfg.get("aliases") or {}).items()]
            await self._write_response(writer, 200, json.dumps(
                {"object": "list", "data": catalog + aliases}, ensure_ascii=False).encode(),
                "application/json", hm)
            return True
        if path == "/v1/embeddings" or path == "/embeddings":
            return await self._embeddings(writer, req, body_json or {}, headers, scenario, hm)
        if path in ("/route", "/admin/route"):
            return await self._route_dry(writer, req, body_json or {}, query, scenario, headers, hm)

        # 推理接口
        if path in ("/v1/chat/completions", "/chat/completions", "/v1/completions",
                    "/completions"):
            return await self._infer(writer, req, body_json or {}, headers, "openai", scenario, hm)
        if path in ("/v1/messages", "/messages"):
            return await self._infer(writer, req, body_json or {}, headers, "anthropic", scenario, hm)
        if path == "/v1/messages/count_tokens":
            return await self._count_tokens(writer, body_json or {}, hm)
        if path in ("/v1/responses", "/responses"):
            return await self._infer(writer, req, body_json or {}, headers, "responses", scenario, hm)

        await self._write_response(writer, 404, json.dumps(
            {"error": {"message": f"未知路径 {path}，访问 GET / 查看全部端点"}},
            ensure_ascii=False).encode(), "application/json", hm)
        return True

    # ── 视图 ────────────────────────────────────────────────────────────────
    def config_view(self, cfg: dict) -> dict:
        """给 UI / 本地应用看的配置视图：绝不包含任何密钥原文。"""
        provs = []
        for p in cfg.get("providers") or []:
            st = self.stats.get(p["id"])
            provs.append({
                "id": p["id"], "label": p.get("label"), "type": p.get("type"),
                "enabled": bool(p.get("enabled")), "base_url": mask_url(p.get("base_url")),
                "models": p.get("models"), "priority": p.get("priority"),
                "tags": p.get("tags"), "rpm": p.get("rpm"), "price": p.get("price"),
                "has_key": bool(p.get("api_key")),
                "missing_env": p.get("_missing_env") or [],
                "requests": st.requests, "errors": st.errors,
                "latency_ms": round(st.latency_ms_ewma, 1),
                "last_error": st.last_error, "last_error_at": st.last_error_at,
            })
        return {
            "version": VERSION,
            "config_path": self.store.path,
            "data_dir": self.stats.data_dir,
            "server": mask_server(cfg.get("server")),
            "provider_types": adapter_types(),   # 面板「服务商类型」下拉的候选
            "providers": provs,
            "rules": cfg.get("rules"),
            "default_route": cfg.get("default_route"),
            "aliases": cfg.get("aliases"),
            "scenarios": list((cfg.get("scenarios") or {}).keys()),
            "guardrails": cfg.get("guardrails"),
            "config_errors": self.store.errors(),
        }

    def health(self) -> dict:
        cfg = self.store.snapshot()
        snap = self.stats.snapshot()
        provs = []
        for p in cfg.get("providers") or []:
            st = snap["providers"].get(p["id"], {})
            provs.append({
                "id": p["id"], "label": p.get("label"), "type": p.get("type"),
                "enabled": bool(p.get("enabled")), "models": p.get("models"),
                "requests": st.get("requests", 0), "errors": st.get("errors", 0),
                "latency_ms": st.get("latency_ms", 0),
                "missing_env": p.get("_missing_env") or [],
                "last_error": st.get("last_error", ""),
                "ok": bool(p.get("enabled") and not (p.get("_missing_env") or [])),
            })
        return {
            "status": "ok", "version": VERSION, "pid": os.getpid(),
            "port": self.port, "host": self.host,
            "uptime_s": round(time.time() - self.started_at, 1),
            "config_path": self.store.path, "data_dir": self.stats.data_dir,
            "config_errors": self.store.errors(),
            "providers": provs,
            "enabled_providers": [p["id"] for p in provs if p["enabled"]],
            "total_requests": snap["total_requests"], "total_errors": snap["total_errors"],
            "total_cost": snap["total_cost"],
            "in_flight": self._in_flight,
        }

    # ── 推理主流程 ──────────────────────────────────────────────────────────
    def _build_canon(self, body: dict, headers: dict, protocol: str, scenario: str,
                     path: str) -> CanonRequest:
        parser = PARSERS.get(protocol, PARSERS["openai"])
        req = parser(body)
        req.hint_scenario = scenario or headers.get("x-llmhub-scenario", "")
        req.hint_provider = headers.get("x-llmhub-provider", "")
        tags = headers.get("x-llmhub-tags", "")
        req.hint_tags = [t.strip() for t in tags.split(",") if t.strip()]
        req.path = path
        return req

    async def _infer(self, writer, raw: dict, body: dict, headers: dict, protocol: str,
                     scenario: str, hm: dict) -> bool:
        t0 = time.time()
        cfg = self.store.snapshot()
        req = self._build_canon(body, headers, protocol, scenario, raw["target"])
        ua = headers.get("user-agent", "")
        if not req.model and protocol != "anthropic":
            await self._write_response(writer, 400, json.dumps(error_payload(
                protocol, "请求缺少 model 字段", "bad_request", 400), ensure_ascii=False).encode(),
                "application/json", hm)
            return True

        blocked = self.executor.apply_guardrails(req, cfg)
        if blocked:
            self.stats.add_log({"ts": time.time(), "path": raw["target"], "model": req.model,
                                "status": 400, "error": blocked, "ms": 0, "protocol": protocol,
                                "agent": ua, "stream": req.stream})
            await self._write_response(writer, 400, json.dumps(error_payload(
                protocol, blocked, "bad_request", 400), ensure_ascii=False).encode(),
                "application/json", hm)
            return True

        dec = self.router.resolve(req, cfg)
        if not dec.candidates:
            msg = "没有匹配到任何可用 provider：" + "；".join(dec.notes)
            await self._write_response(writer, 502, json.dumps(error_payload(
                protocol, msg, "connect", 502), ensure_ascii=False).encode(),
                "application/json", hm)
            return True
        if protocol == "openai" and raw["target"].endswith("/completions") and "chat" not in raw["target"]:
            # 旧版 completions：按 chat 处理，但按 text 格式返回
            return await self._legacy_completion(writer, req, dec, cfg, headers, hm, t0, ua)

        if req.stream:
            return await self._stream(writer, req, dec, cfg, headers, hm, t0, ua)

        loop = asyncio.get_running_loop()
        try:
            resp, meta = await loop.run_in_executor(None, self.executor.complete, req, dec, cfg)
        except Exception as e:  # noqa: BLE001
            status = status_for(e) if isinstance(e, UpstreamError) else 502
            payload = error_payload(protocol, str(e), getattr(e, "kind", "error"), status)
            self.stats.add_log({"ts": time.time(), "path": raw["target"], "model": req.model,
                                "status": status, "error": str(e), "ms": round((time.time() - t0) * 1000, 1),
                                "protocol": protocol, "agent": ua, "stream": False,
                                "rule": dec.rule_id, "attempts": []})
            await self._write_response(writer, status, json.dumps(payload, ensure_ascii=False).encode(),
                                       "application/json", hm)
            return True

        out = serialize_response(resp, req)
        extra = {**hm, "X-LLMHub-Provider": meta.provider, "X-LLMHub-Model": meta.model,
                 "X-LLMHub-Rule": meta.rule, "X-LLMHub-Tactic": meta.tactic,
                 "X-LLMHub-Latency-Ms": str(round(meta.ms, 1))}
        self._log_request(raw, req, meta, 200, ua, t0)
        await self._write_response(writer, 200, json.dumps(out, ensure_ascii=False).encode(),
                                   "application/json", extra)
        return True

    async def _legacy_completion(self, writer, req: CanonRequest, dec: RouteDecision, cfg: dict,
                                 headers: dict, hm: dict, t0: float, ua: str) -> bool:
        loop = asyncio.get_running_loop()
        try:
            resp, meta = await loop.run_in_executor(None, self.executor.complete, req, dec, cfg)
        except Exception as e:  # noqa: BLE001
            status = status_for(e) if isinstance(e, UpstreamError) else 502
            await self._write_response(writer, status, json.dumps(
                error_payload("openai", str(e), getattr(e, "kind", "error"), status),
                ensure_ascii=False).encode(), "application/json", hm)
            return True
        out = {
            "id": resp.id, "object": "text_completion", "created": int(time.time()),
            "model": meta.model,
            "choices": [{"text": resp.text, "index": 0, "logprobs": None,
                         "finish_reason": resp.finish_reason or "stop"}],
            "usage": {"prompt_tokens": resp.prompt_tokens, "completion_tokens": resp.completion_tokens,
                      "total_tokens": resp.prompt_tokens + resp.completion_tokens},
        }
        self._log_request({"target": "/v1/completions"}, req, meta, 200, ua, t0)
        await self._write_response(writer, 200, json.dumps(out, ensure_ascii=False).encode(),
                                   "application/json",
                                   {**hm, "X-LLMHub-Provider": meta.provider,
                                    "X-LLMHub-Model": meta.model})
        return True

    async def _stream(self, writer, req: CanonRequest, dec: RouteDecision, cfg: dict,
                      headers: dict, hm: dict, t0: float, ua: str) -> bool:
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue = asyncio.Queue()
        abort = threading.Event()
        sw = StreamWriter(req, req.model)

        def on_event(ev: dict):
            if abort.is_set():
                raise AbortSignal()
            loop.call_soon_threadsafe(queue.put_nowait, ("ev", ev))

        def worker():
            try:
                meta = self.executor.stream(req, dec, cfg, on_event)
                loop.call_soon_threadsafe(queue.put_nowait, ("done", meta))
            except AbortSignal:
                loop.call_soon_threadsafe(queue.put_nowait, ("abort", None))
            except Exception as e:  # noqa: BLE001
                loop.call_soon_threadsafe(queue.put_nowait, ("err", e))

        fut = loop.run_in_executor(None, worker)
        started = False
        meta = None
        try:
            while True:
                kind, payload = await queue.get()
                if kind == "ev":
                    ev = payload
                    if ev.get("t") == "meta":
                        if not started:
                            started = True
                            await self._start_stream(writer, {
                                **hm, "X-LLMHub-Provider": str(ev.get("provider") or ""),
                                "X-LLMHub-Model": str(ev.get("model") or ""),
                                "X-LLMHub-Rule": str(ev.get("rule") or ""),
                                "X-LLMHub-Tactic": str(ev.get("tactic") or "")})
                        continue
                    if not started:
                        started = True
                        await self._start_stream(writer, hm)
                    for chunk in sw.feed(ev):
                        await self._write_chunk(writer, chunk)
                elif kind == "done":
                    meta = payload
                    break
                elif kind == "err":
                    err = payload
                    req.protocol = req.protocol or "openai"
                    if started:
                        err_payload = error_payload(req.protocol, str(err),
                                                    getattr(err, "kind", "error"), 502)
                        await self._write_chunk(writer, StreamWriter.sse(err_payload))
                    else:
                        status = status_for(err) if isinstance(err, UpstreamError) else 502
                        self.stats.add_log({"ts": time.time(), "path": req.path, "model": req.model,
                                            "status": status, "error": str(err),
                                            "ms": round((time.time() - t0) * 1000, 1),
                                            "protocol": req.protocol, "agent": ua, "stream": True,
                                            "rule": dec.rule_id})
                        await self._write_response(writer, status, json.dumps(
                            error_payload(req.protocol, str(err), getattr(err, "kind", "error"),
                                          status), ensure_ascii=False).encode(),
                            "application/json", hm)
                        abort.set()
                        return True
                    break
                elif kind == "abort":
                    return True
        except (ConnectionResetError, BrokenPipeError, asyncio.CancelledError):
            abort.set()
            try:
                await fut
            except Exception:
                pass
            consumed = dict(zip(["provider", "model", "rule"], ["-", "-", dec.rule_id]))
            self.stats.add_log({"ts": time.time(), "path": req.path, "model": req.model,
                                "status": 499, "error": "客户端断开", "stream": True,
                                "ms": round((time.time() - t0) * 1000, 1), "protocol": req.protocol,
                                "agent": ua, **consumed})
            return False
        finally:
            if not fut.done():
                try:
                    await asyncio.wait_for(fut, timeout=5)
                except Exception:
                    abort.set()
                    fut.cancel()
        if started:
            await self._end_stream(writer)
        if meta is not None:
            self._log_request({"target": req.path}, req, meta, 200, ua, t0)
        return True

    def _log_request(self, raw: dict, req: CanonRequest, meta, status: int, ua: str, t0: float):
        entry = {
            "ts": time.time(),
            "path": raw.get("target") or req.path,
            "protocol": req.protocol,
            "model": req.model,
            "provider": meta.provider,
            "upstream_model": meta.model,
            "rule": meta.rule,
            "tactic": meta.tactic,
            "stream": req.stream,
            "status": status,
            "ms": round(meta.ms, 1),
            "total_ms": round((time.time() - t0) * 1000, 1),
            "tokens_in": meta.tokens_in,
            "tokens_out": meta.tokens_out,
            "cost": round(meta.cost, 6),
            "tools": len(req.tools),
            "fallback": len([a for a in meta.attempts if not a.get("ok")]),
            "attempts": meta.attempts,
            "agent": ua,
        }
        self.stats.add_log(entry)
        if not (self.store.snapshot().get("server") or {}).get("log_requests", True):
            return
        extra = ""
        if entry["fallback"]:
            extra = "  fallback=" + " -> ".join(f"{a['provider']}:{a.get('error', 'ok')}"
                                                for a in meta.attempts)
        self.log(f"{req.protocol} {req.model} → {meta.provider}:{meta.model} "
                 f"[{meta.rule}/{meta.tactic}] {entry['ms']}ms "
                 f"in={meta.tokens_in} out={meta.tokens_out} ${meta.cost:.6f}{extra}")

    # ── 其他接口 ────────────────────────────────────────────────────────────
    async def _route_dry(self, writer, raw: dict, body: dict, query: dict, scenario: str,
                         headers: dict, hm: dict) -> bool:
        cfg = self.store.snapshot()
        if raw["method"] == "GET":
            model = (query.get("model") or [""])[0]
            prompt = (query.get("prompt") or [""])[0]
            body = {"model": model, "messages": [{"role": "user", "content": prompt}],
                    "stream": (query.get("stream") or ["false"])[0].lower() == "true"}
        protocol = "openai"
        if "max_tokens" in body or "system" in body:
            protocol = "anthropic" if "max_tokens" in body and "messages" in body else "openai"
        req = self._build_canon(body, headers, protocol, scenario, raw["target"])
        if not req.model and raw["method"] == "GET":
            req.model = (query.get("model") or ["*"])[0]
        dec = self.router.resolve(req, cfg)
        out = self.executor.dry_run(req, dec, cfg)
        await self._write_response(writer, 200, json.dumps(out, ensure_ascii=False, indent=2).encode(),
                                   "application/json", hm)
        return True

    async def _count_tokens(self, writer, body: dict, hm: dict) -> bool:
        req = PARSERS["anthropic"](body)
        out = {"input_tokens": req.approx_prompt_tokens}
        await self._write_response(writer, 200, json.dumps(out).encode(), "application/json", hm)
        return True

    async def _admin_test(self, writer, body: dict, hm: dict) -> bool:
        cfg = self.store.snapshot()
        provider = str(body.get("provider") or "")
        model = str(body.get("model") or "")
        prompt = str(body.get("prompt") or "ping")
        req = CanonRequest(protocol="openai", model=model or "ping",
                           messages=[Msg(role="user", parts=[Part(type="text", text=prompt)])],
                           max_tokens=int(body.get("max_tokens") or 32),
                           hint_provider=provider)
        dec = self.router.resolve(req, cfg)
        if provider:
            dec.candidates = [c for c in dec.candidates if c.provider_id == provider] or dec.candidates
        loop = asyncio.get_running_loop()
        t0 = time.time()
        try:
            resp, meta = await loop.run_in_executor(None, self.executor.complete, req, dec, cfg)
            out = {"ok": True, "provider": meta.provider, "model": meta.model,
                   "text": resp.text[:500], "ms": round(meta.ms, 1),
                   "tokens_in": resp.prompt_tokens, "tokens_out": resp.completion_tokens,
                   "attempts": meta.attempts}
        except Exception as e:  # noqa: BLE001
            out = {"ok": False, "error": str(e), "kind": getattr(e, "kind", "error"),
                   "ms": round((time.time() - t0) * 1000, 1)}
        await self._write_response(writer, 200, json.dumps(out, ensure_ascii=False).encode(),
                                   "application/json", hm)
        return True

    async def _admin_ask(self, writer, body: dict, headers: dict, hm: dict) -> bool:
        """给本地应用的最简接口：POST /admin/ask {"prompt": "..."} → {"text": "..."}"""
        prompt = body.get("prompt")
        messages = body.get("messages")
        if not messages:
            if prompt is None:
                await self._write_response(writer, 400, json.dumps(
                    {"error": {"message": "需要 prompt 或 messages 字段"}}).encode(),
                    "application/json", hm)
                return True
            if isinstance(prompt, list):
                messages = [{"role": "user", "content": p} for p in prompt]
            else:
                messages = [{"role": "user", "content": str(prompt)}]
        model = body.get("model") or "default"
        oai = {"model": model, "messages": messages, "stream": False,
               "max_tokens": int(body.get("max_tokens") or 1024)}
        if body.get("system"):
            oai["messages"] = [{"role": "system", "content": body["system"]}] + oai["messages"]
        hh = dict(headers)
        if body.get("provider"):
            hh["x-llmhub-provider"] = str(body["provider"])
        if body.get("scenario"):
            hh["x-llmhub-scenario"] = str(body["scenario"])
        return await self._ask_inner(writer, oai, hh, hm, body)

    async def _ask_inner(self, writer, oai: dict, headers: dict, hm: dict, body: dict) -> bool:
        cfg = self.store.snapshot()
        t0 = time.time()
        ua = headers.get("user-agent", "")
        req = self._build_canon(oai, headers, "openai", oai.get("scenario", ""), "/admin/ask")
        dec = self.router.resolve(req, cfg)
        loop = asyncio.get_running_loop()
        try:
            resp, meta = await loop.run_in_executor(None, self.executor.complete, req, dec, cfg)
        except Exception as e:  # noqa: BLE001
            status = status_for(e) if isinstance(e, UpstreamError) else 502
            await self._write_response(writer, status, json.dumps(
                {"error": {"message": str(e), "code": getattr(e, "kind", "error")}},
                ensure_ascii=False).encode(), "application/json", hm)
            return True
        out = {"text": resp.text, "provider": meta.provider, "model": meta.model, "rule": meta.rule,
               "tactic": meta.tactic, "ms": round(meta.ms, 1),
               "usage": {"prompt_tokens": resp.prompt_tokens,
                         "completion_tokens": resp.completion_tokens},
               "cost": round(meta.cost, 6),
               "tool_calls": resp.tool_calls, "attempts": meta.attempts}
        self._log_request({"target": "/admin/ask"}, req, meta, 200, ua, t0)
        await self._write_response(writer, 200, json.dumps(out, ensure_ascii=False).encode(),
                                   "application/json",
                                   {**hm, "X-LLMHub-Provider": meta.provider,
                                    "X-LLMHub-Model": meta.model})
        return True

    async def _embeddings(self, writer, raw: dict, body: dict, headers: dict, scenario: str,
                          hm: dict) -> bool:
        cfg = self.store.snapshot()
        model = str(body.get("model") or "")
        req = CanonRequest(protocol="openai", model=model, max_tokens=0)
        req.hint_provider = headers.get("x-llmhub-provider", "")
        req.hint_scenario = scenario
        dec = self.router.resolve(req, cfg)
        loop = asyncio.get_running_loop()

        def run():
            last = None
            for cand in dec.candidates:
                p = None
                for q in cfg.get("providers") or []:
                    if q["id"] == cand.provider_id:
                        p = q
                if p is None:
                    continue
                base = (p.get("base_url") or "").rstrip("/")
                payload = {"input": body.get("input"), "model": cand.model}
                if body.get("encoding_format"):
                    payload["encoding_format"] = body["encoding_format"]
                hdrs = {"Content-Type": "application/json"}
                if p.get("api_key"):
                    hdrs["Authorization"] = f"Bearer {p['api_key']}"
                hdrs.update({k: v for k, v in (p.get("headers") or {}).items()})
                try:
                    status, _h, data = http_request(HttpRequest(
                        url=base + "/v1/embeddings", headers=hdrs,
                        body=json.dumps(payload).encode(),
                        timeout_ms=int(p.get("timeout_ms") or 60000)))
                    check_status(status, data)
                    return data
                except Exception as e:  # noqa: BLE001
                    last = e
            raise last or UpstreamError("没有可用的 embeddings provider", kind="connect")

        try:
            data = await loop.run_in_executor(None, run)
            await self._write_response(writer, 200, data, "application/json", hm)
        except Exception as e:  # noqa: BLE001
            status = status_for(e) if isinstance(e, UpstreamError) else 502
            await self._write_response(writer, status, json.dumps(
                error_payload("openai", str(e), getattr(e, "kind", "error"), status),
                ensure_ascii=False).encode(), "application/json", hm)
        return True


def pick_port(preferred: int) -> int:
    """端口被占用时自动 +1 重试。"""
    for offset in range(0, 20):
        port = preferred + offset
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                s.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    return preferred
