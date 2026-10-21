#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""llmhubd —— 大模型控制中心命令行：守护进程管理 + 本地调用接口。

用法速览：
    llmhubd start / stop / restart / status / health
    llmhubd ask "你好"            # 最简本地调用（自动选 provider）
    llmhubd ask -m gpt-4o-mini --provider openai "…"
    llmhubd chat                  # 交互式终端
    llmhubd route -m mock-echo -p "hi"   # 规则干跑：看会走哪个 provider
    llmhubd models / providers / stats / logs / test <id> / enable <id> / disable <id>
    llmhubd init / doctor / config / port
"""
from __future__ import annotations

import argparse
import json
import os
import re
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

from llmhub_core import (DEFAULT_CONFIG, VERSION, ConfigStore, default_config_path,  # noqa: E402
                         default_data_dir, expand_env, model_catalog)

BIN_NAME = "llmhubd"


# ── 数据目录 / 文件 ─────────────────────────────────────────────────────────
def write_int(path: str, value: int) -> None:
    """写回 pid/端口这类运行时文件（文件丢了但网关还活着时用来补齐）。"""
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(str(int(value)))
    except OSError:
        pass


def expected_port(args) -> int:
    """先看端口文件，再退回配置里的 server.port，最后用内置默认值。"""
    port = read_int(port_file(args))
    if port:
        return port
    try:
        c = ConfigStore(args.config or default_config_path()).snapshot()
        return int((c.get("server") or {}).get("port") or DEFAULT_CONFIG["server"]["port"])
    except Exception:
        return int(DEFAULT_CONFIG["server"]["port"])


def probe_health(args, timeout: float = 3.0):
    """不依赖 pid/端口文件探测网关：/health 通就返回它的 JSON（/health 不需要鉴权）。

    这是「网关其实活着、但运行时文件丢了」时的救命通道 —— 否则 CLI 会一口咬定
    「网关未运行」，面板上的停止/重启按钮也就点了没反应。
    """
    url = f"http://127.0.0.1:{expected_port(args)}/health"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return json.loads(r.read().decode() or "{}")
    except Exception:
        return None


def sync_runtime_files(args, health: dict) -> None:
    """按 /health 的内容补齐 pid/端口文件（网关自己报的 pid 最可信）。"""
    pid = int(health.get("pid") or 0)
    port = int(health.get("port") or 0)
    if pid and not pid_alive(read_int(pid_file(args))):
        write_int(pid_file(args), pid)
    if port:
        write_int(port_file(args), port)


def data_dir(args) -> str:
    d = getattr(args, "data_dir", None) or os.environ.get("LLMHUB_DATA_DIR") or default_data_dir()
    os.makedirs(d, exist_ok=True)
    return d


def pid_file(args) -> str:
    return os.path.join(data_dir(args), "llmhubd.pid")


def port_file(args) -> str:
    return os.path.join(data_dir(args), "llmhubd.port")


def log_file(args) -> str:
    return os.path.join(data_dir(args), "llmhubd.log")


def read_int(path: str) -> int:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return int((f.read() or "0").strip() or 0)
    except Exception:
        return 0


def pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


# ── HTTP 客户端 ─────────────────────────────────────────────────────────────
class Local:
    def __init__(self, args):
        self.args = args
        self.port = read_int(port_file(args))
        if not self.port:
            # 运行时文件丢了但网关可能在跑：探一次 /health 并补回端口文件
            h = probe_health(args)
            if h:
                sync_runtime_files(args, h)
                self.port = int(h.get("port") or 0) or expected_port(args)
        if not self.port:
            raise SystemExit("网关未运行（读不到端口文件）。先执行：llmhubd start")

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def call(self, path: str, body: dict | None = None, timeout: float = 600.0,
             method: str | None = None, raw: bool = False):
        url = self.base + path
        data = None
        headers = {"Content-Type": "application/json"}
        key = os.environ.get("LLMHUB_API_KEY", "")
        c = None
        try:
            c = ConfigStore(self.args.config or default_config_path()).snapshot()
            key = key or str((c.get("server") or {}).get("api_key") or "")
        except Exception:
            pass
        if key:
            headers["Authorization"] = f"Bearer {key}"
        if body is not None:
            data = json.dumps(body).encode()
        req = urllib.request.Request(url, data=data,
                                    headers=headers, method=method or ("POST" if data else "GET"))
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                payload = r.read()
                return payload if raw else json.loads(payload.decode() or "{}")
        except urllib.error.HTTPError as e:
            payload = e.read()
            if raw:
                raise
            try:
                return {"_http_error": e.code, **json.loads(payload.decode() or "{}")}
            except Exception:
                return {"_http_error": e.code, "error": {"message": payload.decode("utf-8", "replace")}}
        except urllib.error.URLError as e:
            raise SystemExit(f"无法连接网关 {self.base}：{e.reason}")

    def stream(self, path: str, body: dict):
        """SSE 流式调用，逐行打印增量文本。"""
        url = self.base + path
        body = {**body, "stream": True}
        headers = {"Content-Type": "application/json", "Accept": "text/event-stream"}
        c = None
        try:
            c = ConfigStore(self.args.config or default_config_path()).snapshot()
        except Exception:
            pass
        key = os.environ.get("LLMHUB_API_KEY", "") or str(
            ((c or {}).get("server") or {}).get("api_key") or "")
        if key:
            headers["Authorization"] = f"Bearer {key}"
        req = urllib.request.Request(url, data=json.dumps(body).encode(), headers=headers, method="POST")
        chunks: list[str] = []
        meta: dict = {}
        with urllib.request.urlopen(req, timeout=900) as r:
            meta = {k[10:]: v for k, v in r.headers.items() if k.lower().startswith("x-llmhub-")}
            for raw in r:
                line = raw.decode("utf-8", "replace").rstrip("\r\n")
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data in ("", "[DONE]"):
                    continue
                try:
                    ev = json.loads(data)
                except Exception:
                    continue
                txt = _delta_text(ev)
                if txt:
                    chunks.append(txt)
                    sys.stdout.write(txt)
                    sys.stdout.flush()
        sys.stdout.write("\n")
        return "".join(chunks), meta


def _delta_text(ev: dict) -> str:
    if ev.get("type") == "content_block_delta":
        d = ev.get("delta") or {}
        return d.get("text") or ""
    if ev.get("type") == "response.output_text.delta":
        return ev.get("delta") or ""
    for ch in ev.get("choices") or []:
        d = ch.get("delta") or {}
        if d.get("content"):
            return d["content"]
    return ""


# ── 子命令 ──────────────────────────────────────────────────────────────────
def cmd_serve(args) -> int:
    import llmhub_server as srv
    from llmhub_core import ConfigStore, Executor, Router, Stats

    cfg_path = args.config or default_config_path()
    store = ConfigStore(cfg_path)
    if not os.path.exists(cfg_path):
        store.save(dict(DEFAULT_CONFIG))
        print(f"[init] 已创建默认配置：{cfg_path}")
    cfg = store.snapshot()
    host = args.host or (cfg.get("server") or {}).get("host") or "127.0.0.1"
    want = args.port if args.port is not None else int((cfg.get("server") or {}).get("port") or 8787)
    port = srv.pick_port(want)
    stats = Stats(data_dir(args), log_limit=int((cfg.get("server") or {}).get("max_log_entries") or 500))
    router = Router(store, stats)
    executor = Executor(store, stats, router)
    gw = srv.Gateway(store, stats, executor, router, quiet=args.quiet)

    with open(pid_file(args), "w", encoding="utf-8") as f:
        f.write(str(os.getpid()))
    with open(port_file(args), "w", encoding="utf-8") as f:
        f.write(str(port))
    if port != want:
        print(f"[port] {want} 被占用，改用 {port}")
    try:
        import asyncio
        asyncio.run(gw.serve(host, port))
    finally:
        for p in (pid_file(args), port_file(args)):
            try:
                os.remove(p)
            except OSError:
                pass
    return 0


def cmd_start(args) -> int:
    pid = read_int(pid_file(args))
    if pid_alive(pid):
        print(f"网关已在运行（pid {pid}，端口 {read_int(port_file(args))}）")
        return 0
    # pid 文件丢了但网关其实活着（运行时文件被清过 / 换过数据目录）：
    # 补文件后直接返回，绝不能再拉一个 —— 那样端口被占会自动落到 8788。
    h = probe_health(args)
    if h:
        sync_runtime_files(args, h)
        print(f"网关已在运行：http://127.0.0.1:{h.get('port')}  "
              f"(pid {h.get('pid')}，运行时文件已补齐)")
        return 0
    lf = log_file(args)
    # -u：让 stdout 无缓冲，否则请求日志要等缓冲区满才落到 llmhubd.log
    cmd = [sys.executable, "-u", os.path.join(HERE, "llmhubd.py"), "serve"]
    if args.config:
        cmd += ["--config", args.config]
    port = getattr(args, "port", None)   # restart 子命令没有 --port/--quiet
    if port is not None:
        cmd += ["--port", str(port)]
    if getattr(args, "quiet", False):
        cmd.append("--quiet")
    with open(lf, "ab") as fh:
        fh.write(f"\n=== start {time.strftime('%Y-%m-%d %H:%M:%S')} ===\n".encode())
        fh.flush()
        p = subprocess.Popen(cmd, stdout=fh, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                             start_new_session=True,
                             env={**os.environ, "LLMHUB_DATA_DIR": data_dir(args)},
                             cwd=HERE)
    for _ in range(60):
        time.sleep(0.2)
        pid = read_int(pid_file(args))
        port = read_int(port_file(args))
        if pid_alive(pid) and port:
            try:
                g = Local(args)
                h = g.call("/health", timeout=3)
                print(f"✓ 网关已启动：http://127.0.0.1:{port}  (pid {pid}, "
                      f"{len(h.get('enabled_providers') or [])} 个 provider 启用)")
                return 0
            except SystemExit:
                continue
    if not pid_alive(read_int(pid_file(args))):
        print(f"✗ 启动失败，日志尾部（{lf}）：")
        tail(lf, 20)
        return 1
    print("网关进程在运行，但健康检查未通过；详见日志：", lf)
    return 1


def cmd_stop(args) -> int:
    pid = read_int(pid_file(args))
    if not pid_alive(pid):
        # pid 文件可能是丢的：用 /health 兜底问出真进程
        h = probe_health(args)
        if h and h.get("pid"):
            pid = int(h["pid"])
            sync_runtime_files(args, h)
            print(f"pid 文件已失效，按 /health 报告的 pid {pid} 停止")
        else:
            print("网关未在运行")
            for p in (pid_file(args), port_file(args)):
                try:
                    os.remove(p)
                except OSError:
                    pass
            return 0
    os.kill(pid, signal.SIGTERM)
    for _ in range(50):
        time.sleep(0.1)
        if not pid_alive(pid):
            print(f"✓ 已停止（pid {pid}）")
            return 0
    os.kill(pid, signal.SIGKILL)
    print(f"✓ 已强制停止（pid {pid}）")
    return 0


def cmd_restart(args) -> int:
    cmd_stop(args)
    time.sleep(0.3)
    return cmd_start(args)


def cmd_status(args) -> int:
    pid = read_int(pid_file(args))
    port = read_int(port_file(args))
    alive = pid_alive(pid)
    if not alive:
        h = probe_health(args)
        if h:
            sync_runtime_files(args, h)
            pid = int(h.get("pid") or 0)
            port = int(h.get("port") or 0)
            alive = True
    print(f"进程：{'运行中 pid ' + str(pid) if alive else '未运行'}")
    print(f"端口：{port or '-'}")
    print(f"配置：{args.config or default_config_path()}")
    print(f"数据：{data_dir(args)}")
    print(f"日志：{log_file(args)}")
    if not (alive and port):
        return 1
    try:
        h = Local(args).call("/health", timeout=5)
    except SystemExit as e:
        print(f"健康检查失败：{e}")
        return 1
    print(f"网关：v{h.get('version')}  运行 {h.get('uptime_s')}s  "
          f"请求 {h.get('total_requests')}  错误 {h.get('total_errors')}  "
          f"成本 ${h.get('total_cost', 0)}\n")
    print(f"{'provider':<16}{'类型':<12}{'启用':<6}{'请求':<7}{'错误':<6}{'延迟ms':<9}模型")
    for p in h.get("providers") or []:
        models = ",".join((p.get("models") or [])[:3])
        flag = "✓" if p.get("enabled") else "-"
        warn = "  ⚠ 缺环境变量 " + ",".join(p.get("missing_env") or []) if p.get("missing_env") else ""
        print(f"{p['id']:<16}{(p.get('type') or ''):<12}{flag:<6}{p.get('requests', 0):<7}"
              f"{p.get('errors', 0):<6}{p.get('latency_ms', 0):<9}{models}{warn}")
    return 0


def cmd_health(args) -> int:
    print(json.dumps(Local(args).call("/health", timeout=10), ensure_ascii=False, indent=2))
    return 0


def cmd_ask(args) -> int:
    g = Local(args)
    prompt = args.prompt
    if prompt is None:
        prompt = sys.stdin.read()
    body = {"prompt": prompt, "max_tokens": args.max_tokens}
    if args.model:
        body["model"] = args.model
    if args.provider:
        body["provider"] = args.provider
    if args.scenario:
        body["scenario"] = args.scenario
    if args.system:
        body["system"] = args.system
    if args.stream:
        text, meta = g.stream("/v1/chat/completions",
                              {"model": args.model or "default",
                               "messages": ([{"role": "system", "content": args.system}] if args.system else [])
                                           + [{"role": "user", "content": prompt}],
                               "max_tokens": args.max_tokens})
        if args.json:
            print(json.dumps({"text": text, "meta": meta}, ensure_ascii=False))
        return 0
    out = g.call("/admin/ask", body)
    if args.json:
        print(json.dumps(out, ensure_ascii=False, indent=2))
    elif out.get("text") is not None:
        print(out["text"])
        if args.verbose:
            print(f"\n--- {out.get('provider')}:{out.get('model')} [{out.get('rule')}/"
                  f"{out.get('tactic')}] {out.get('ms')}ms "
                  f"tokens {out['usage']['prompt_tokens']}+{out['usage']['completion_tokens']} "
                  f"${out.get('cost')}", file=sys.stderr)
    else:
        print(json.dumps(out, ensure_ascii=False, indent=2), file=sys.stderr)
        return 1
    return 0


def cmd_chat(args) -> int:
    g = Local(args)
    hist: list[dict] = []
    print(f"llm-hub 交互终端（模型 {args.model or 'default'}，/exit 退出，/clear 清空上下文）")
    while True:
        try:
            line = input("› ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not line:
            continue
        if line in ("/exit", "/quit"):
            break
        if line == "/clear":
            hist.clear()
            print("已清空上下文")
            continue
        hist.append({"role": "user", "content": line})
        body = {"messages": hist, "model": args.model or "default", "max_tokens": args.max_tokens}
        if args.provider:
            body["provider"] = args.provider
        out = g.call("/admin/ask", body)
        text = out.get("text")
        if text is None:
            print(json.dumps(out, ensure_ascii=False), file=sys.stderr)
            hist.pop()
            continue
        print(text)
        print(f"  [{out.get('provider')}:{out.get('model')} {out.get('ms')}ms]", file=sys.stderr)
        hist.append({"role": "assistant", "content": text})
    return 0


def cmd_route(args) -> int:
    g = Local(args)
    q = f"/route?model={urllib.parse.quote(args.model or '*')}"
    if args.prompt:
        q += "&prompt=" + urllib.parse.quote(args.prompt)
    out = g.call(q)
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


def cmd_models(args) -> int:
    g = Local(args)
    out = g.call("/v1/models")
    data = out.get("data") or []
    if args.json:
        print(json.dumps(data, ensure_ascii=False, indent=2))
        return 0
    print(f"共 {len(data)} 个模型条目\n")
    print(f"{'模型':<34}{'provider':<22}{'启用':<6}请求")
    for m in data:
        provs = ",".join(m.get("providers") or [])
        print(f"{(m.get('id') or '')[:33]:<34}{provs[:21]:<22}"
              f"{'✓' if m.get('enabled') else '-':<6}{m.get('requests', 0)}")
    return 0


def cmd_providers(args) -> int:
    out = Local(args).call("/config")
    ps = out.get("providers") or []
    if args.json:
        print(json.dumps(ps, ensure_ascii=False, indent=2))
        return 0
    print(f"{'id':<16}{'类型':<12}{'启用':<6}{'模型':<28}{'优先级':<8}密钥/备注")
    for p in ps:
        note = []
        if not p.get("has_key"):
            note.append("无密钥")
        if p.get("missing_env"):
            note.append("缺 $" + ",$".join(p["missing_env"]))
        if p.get("last_error"):
            note.append("上次错误: " + str(p["last_error"])[:40])
        print(f"{p['id']:<16}{(p.get('type') or ''):<12}{'✓' if p.get('enabled') else '-':<6}"
              f"{','.join(p.get('models') or [])[:27]:<28}{str(p.get('priority', 100)):<8}"
              + " ".join(note))
    rules = out.get("rules") or []
    print(f"\n规则（{len(rules)} 条，自上而下匹配）：")
    for r in rules:
        rt = r.get("route") if isinstance(r.get("route"), dict) else r
        specs = rt.get("providers") or []
        tactic = rt.get("tactic") or "priority"
        shown = " → ".join(str(s) for s in specs) if specs else "（按 tactic 从候选池选）"
        print(f"  · {r.get('id')}: when={json.dumps(r.get('when'), ensure_ascii=False)} "
              f"→ {shown} [{tactic}]")
    dr = out.get("default_route") or {}
    ds = dr.get("providers") or []
    dshown = " → ".join(str(s) for s in ds) if ds else "（全部 provider）"
    print(f"默认路由：{dshown} [{dr.get('tactic')}] "
          f"fallback={dr.get('fallback')}")
    return 0


def cmd_toggle(args, enabled: bool) -> int:
    out = Local(args).call(f"/admin/providers/{args.id}/toggle", {"enabled": enabled})
    print(json.dumps(out, ensure_ascii=False))
    return 0 if out.get("ok") else 1


def cmd_test(args) -> int:
    g = Local(args)
    body = {"provider": args.id, "prompt": args.prompt, "max_tokens": args.max_tokens}
    if args.model:
        body["model"] = args.model

    # --json 时人类可读行一律走 stderr，stdout 只留一个 JSON 对象；
    # 否则调用方（面板 hub.cliJson）把整段 stdout 当 JSON 解析必然失败，
    # 从而回落成「无法连接」这类兜底文案。
    def say(line: str) -> None:
        print(line, file=sys.stderr if args.json else sys.stdout)

    try:
        out = g.call("/admin/test", body, timeout=180)
    except SystemExit as exc:  # 网关没起来 / 端口不通，也给出可解析的结构化结果
        out = {"ok": False, "kind": "connect", "provider": args.id,
               "error": str(exc)}
    out.setdefault("provider", args.id)  # 失败体也带上请求的 provider，便于调用方归因
    if out.get("ok"):
        say(f"✓ {out['provider']}:{out['model']} {out['ms']}ms "
            f"tokens {out['tokens_in']}+{out['tokens_out']}")
        say(out.get("text", ""))
        rc = 0
    else:
        say(f"✗ {out.get('kind') or 'error'}: {out.get('error')}")
        rc = 1
    if args.json:
        print(json.dumps(out, ensure_ascii=False, indent=2))
    return rc


def cmd_stats(args) -> int:
    out = Local(args).call("/stats")
    if args.json:
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return 0
    print(f"运行 {out.get('uptime_s')}s  请求 {out.get('total_requests')}  错误 "
          f"{out.get('total_errors')}  回退 {out.get('total_fallbacks')}  "
          f"tokens {out.get('total_tokens_in')}+{out.get('total_tokens_out')}  "
          f"成本 ${round(out.get('total_cost', 0), 6)}\n")
    print(f"{'provider':<16}{'请求':<7}{'成功':<7}{'错误':<7}{'回退':<7}{'tokens in':<12}"
          f"{'tokens out':<12}{'成本$':<12}延迟ms")
    for pid, s in (out.get("providers") or {}).items():
        print(f"{pid:<16}{s.get('requests', 0):<7}{s.get('ok', 0):<7}{s.get('errors', 0):<7}"
              f"{s.get('fallbacks', 0):<7}{s.get('tokens_in', 0):<12}{s.get('tokens_out', 0):<12}"
              f"{round(s.get('cost', 0), 6):<12}{s.get('latency_ms', 0)}")
    by_model = out.get("by_model") or {}
    if by_model:
        print("\n按模型（命中次数）：")
        for m, v in by_model.items():
            if isinstance(v, dict):
                print(f"  {m:<34}请求 {v.get('requests', 0):<6} tokens "
                      f"{v.get('tokens_in', 0)}+{v.get('tokens_out', 0)}  ${round(v.get('cost', 0), 6)}")
            else:
                print(f"  {m:<34}请求 {v}")
    by_rule = out.get("by_rule") or {}
    if by_rule:
        print("\n按规则：" + "  ".join(f"{k}={v}" for k, v in by_rule.items()))
    return 0


def tail(path: str, n: int):
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
        for line in lines[-n:]:
            print(line.rstrip())
    except OSError as e:
        print(f"（无法读取日志 {path}: {e}）")


def cmd_logs(args) -> int:
    if args.json:
        out = Local(args).call(f"/logs?limit={args.limit}")
        print(json.dumps(out, ensure_ascii=False))
        return 0
    if args.follow:
        try:
            subprocess.run(["tail", "-f", "-n", str(args.limit), log_file(args)])
        except KeyboardInterrupt:
            pass
        return 0
    if args.remote:
        out = Local(args).call(f"/logs?limit={args.limit}")
        for e in out.get("logs") or []:
            ts = time.strftime("%H:%M:%S", time.localtime(e.get("ts") or 0))
            print(f"{ts}  {e.get('status')}  {e.get('model')} → {e.get('provider')}  "
                  f"{e.get('ms')}ms  in={e.get('tokens_in')} out={e.get('tokens_out')}  "
                  f"{e.get('path')}{('  ERR ' + str(e.get('error'))) if e.get('error') else ''}")
        return 0
    tail(log_file(args), args.limit)
    return 0


def cmd_clear_logs(args) -> int:
    print(json.dumps(Local(args).call("/admin/logs/clear"), ensure_ascii=False))
    return 0


def cmd_reset_stats(args) -> int:
    out = Local(args).call("/admin/stats/reset", {"keep_cumulative": not args.all})
    print(json.dumps(out, ensure_ascii=False))
    return 0


def cmd_reload(args) -> int:
    print(json.dumps(Local(args).call("/admin/reload"), ensure_ascii=False, indent=2))
    return 0


def cmd_init(args) -> int:
    path = args.config or default_config_path()
    if os.path.exists(path) and not args.force:
        print(f"配置已存在：{path}（加 --force 覆盖）")
        return 0
    store = ConfigStore(path)
    cfg = store.snapshot() if os.path.exists(path) and not args.force else dict(DEFAULT_CONFIG)
    store.save(cfg)
    print(f"✓ 已写入配置模板：{path}")
    print("  提示：把某个 provider 的 enabled 改为 true 并填 api_key，或在环境变量里放 "
          "OPENAI_API_KEY / DEEPSEEK_API_KEY / ANTHROPIC_API_KEY（配置里写 ${VAR} 引用）")
    return 0


def _coerce(raw: str):
    """把命令行字符串转成配置里合适的类型。"""
    s = raw.strip()
    if s.lower() in ("true", "on", "yes"):
        return True
    if s.lower() in ("false", "off", "no"):
        return False
    if s.lower() in ("null", "none"):
        return None
    body = s[1:] if s[:1] in ("-", "+") else s
    if body.isdigit():
        return int(s)
    return raw


def _key_of(container, part: str):
    """把路径段转成实际可用于 dict/list 的键；不适用时返回 None。

    列表除了下标，也接受条目里的 "id"（providers.mock.base_url）。
    """
    if isinstance(container, list):
        if part.lstrip("-").isdigit():
            return int(part)
        for i, item in enumerate(container):
            if isinstance(item, dict) and str(item.get("id", "")) == part:
                return i
        return None
    return part


def _bad_segment(container, part: str) -> str:
    if isinstance(container, list):
        return f"列表里没有 id 为 '{part}' 的条目，'{part}' 也不是合法下标"
    return f"路径段 '{part}' 不能用于 {type(container).__name__}"


def _set_path(cfg, dotted: str, value) -> None:
    parts = [p for p in dotted.split(".") if p]
    if not parts:
        raise ValueError("空键名")
    cur = cfg
    for p in parts[:-1]:
        key = _key_of(cur, p)
        if key is None:
            raise ValueError(_bad_segment(cur, p))
        if isinstance(cur, list):
            if key >= len(cur):
                raise ValueError(f"下标越界：{p}")
            nxt = cur[key]
        else:
            nxt = cur.get(p)
        if not isinstance(nxt, (dict, list)):
            nxt = {}
            cur[key] = nxt
        cur = nxt
    last = _key_of(cur, parts[-1])
    if last is None:
        raise ValueError(_bad_segment(cur, parts[-1]))
    if isinstance(cur, list):
        if last >= len(cur):
            raise ValueError(f"下标越界：{parts[-1]}")
        cur[last] = value
    else:
        cur[last] = value


def _del_path(cfg, dotted: str):
    """删除配置里的一个键（列表下标 / id 均可），返回是否真删掉了东西。"""
    parts = [p for p in dotted.split(".") if p]
    if not parts:
        raise ValueError("空键名")
    cur = cfg
    for p in parts[:-1]:
        key = _key_of(cur, p)
        if key is None:
            return False
        if isinstance(cur, list):
            if key >= len(cur):
                return False
            cur = cur[key]
        else:
            if p not in cur or not isinstance(cur[p], (dict, list)):
                return False
            cur = cur[p]
    last = _key_of(cur, parts[-1])
    if last is None:
        return False
    if isinstance(cur, list):
        if last >= len(cur):
            return False
        cur.pop(last)
        return True
    if last in cur:
        del cur[last]
        return True
    return False


def cmd_set(args) -> int:
    """改配置里的任意一个键：`llmhubd set server.port 8899`。

    `--json null` 表示删除该键（面板把筛选条件设回「不限」时用）。
    """
    path = args.config or default_config_path()
    store = ConfigStore(path)
    if not os.path.exists(path):
        store.save(dict(DEFAULT_CONFIG))
    if args.json:
        try:
            value = json.loads(args.value)
        except Exception as e:
            print(f"✗ --json 值不是合法 JSON：{e}")
            return 2
    else:
        value = _coerce(args.value)
    removed = {"hit": False}
    try:
        if value is None:
            def fn(cfg):
                removed["hit"] = _del_path(cfg, args.key)
            store.mutate(fn)
        else:
            store.mutate(lambda cfg: _set_path(cfg, args.key, value))
    except ValueError as e:
        print(f"✗ {e}")
        return 2
    cur = _get_path(store.snapshot(), args.key)
    print(json.dumps({"ok": True, "key": args.key, "value": cur,
                      "removed": bool(value is None and removed["hit"]), "path": path},
                     ensure_ascii=False))
    return 0


def _get_path(cfg, dotted: str):
    cur = cfg
    for p in [p for p in dotted.split(".") if p]:
        key = _key_of(cur, p)
        if key is None:
            return None
        try:
            cur = cur[key]
        except (KeyError, IndexError, TypeError):
            return None
    return cur

def cmd_provider_add(args) -> int:
    """新增一个 provider（面板「添加服务商」按钮调它）。"""
    path = args.config or default_config_path()
    store = ConfigStore(path)
    if not os.path.exists(path):
        store.save(dict(DEFAULT_CONFIG))
    models = [m for m in (args.model or []) if m]
    created = {"id": ""}

    def fn(cfg):
        provs = cfg.setdefault("providers", [])
        pid = (args.id or "").strip()
        if not pid:
            n = 1
            used = {str(p.get("id", "")) for p in provs}
            while f"provider-{n}" in used:
                n += 1
            pid = f"provider-{n}"
        if any(str(p.get("id", "")) == pid for p in provs):
            raise ValueError(f"provider id '{pid}' 已存在")
        entry = {
            "id": pid,
            "label": args.label or pid,
            "type": args.type or "openai",
            "enabled": not args.disabled,
            "base_url": args.base_url or "",
            "api_key": args.key or "",
            "models": models,
        }
        if args.priority is not None:
            entry["priority"] = args.priority
        provs.append(entry)
        created["id"] = pid

    try:
        store.mutate(fn)
    except ValueError as e:
        print(f"✗ {e}")
        return 2
    cfg = store.snapshot()
    index = next((i for i, p in enumerate(cfg.get("providers") or [])
                  if str(p.get("id")) == created["id"]), -1)
    print(json.dumps({"ok": True, "id": created["id"], "index": index,
                      "type": args.type or "openai", "models": models,
                      "enabled": not args.disabled, "path": path}, ensure_ascii=False))
    return 0


def cmd_provider_remove(args) -> int:
    """按 id 或下标删除 provider。"""
    path = args.config or default_config_path()
    store = ConfigStore(path)
    target = str(args.target).strip()
    removed = {"id": ""}

    def fn(cfg):
        provs = cfg.get("providers") or []
        idx = None
        if target.lstrip("-").isdigit():
            i = int(target)
            if -len(provs) <= i < len(provs):
                idx = i
        else:
            idx = next((i for i, p in enumerate(provs)
                        if str(p.get("id", "")) == target), None)
        if idx is None:
            raise ValueError(f"没有找到 provider：{target}")
        removed["id"] = str(provs[idx].get("id", ""))
        provs.pop(idx)
        cfg["providers"] = provs

    try:
        store.mutate(fn)
    except ValueError as e:
        print(f"✗ {e}")
        return 2
    print(json.dumps({"ok": True, "removed": removed["id"], "path": path}, ensure_ascii=False))
    return 0


def _csv(raw) -> list:
    """把 `a,b,c` 或可重复参数转成干净列表。"""
    if raw is None:
        return []
    items = raw if isinstance(raw, (list, tuple)) else [raw]
    out: list[str] = []
    for it in items:
        for piece in str(it).replace("，", ",").split(","):
            piece = piece.strip()
            if piece and piece not in out:
                out.append(piece)
    return out


TACTICS = ("listed", "priority", "cheapest", "fastest", "least_used", "random", "round_robin")


def _tactic(raw) -> str:
    """校验排序策略名（路由器的未知策略会静默退化为 priority，这里直接报错更清楚）。"""
    name = (raw or "priority").strip().lower()
    if name not in TACTICS:
        raise ValueError(f"未知排序策略 '{raw}'；可用：{'/'.join(TACTICS)}")
    return name


def _rule_index(rules: list, target: str):
    """按 id 或下标定位一条规则；找不到返回 None。"""
    t = str(target).strip()
    if t.lstrip("-").isdigit():
        i = int(t)
        return i if -len(rules) <= i < len(rules) else None
    return next((i for i, r in enumerate(rules) if str(r.get("id", "")) == t), None)


def cmd_rule_add(args) -> int:
    """新增一条路由规则（面板「添加规则」按钮调它）。"""
    path = args.config or default_config_path()
    store = ConfigStore(path)
    if not os.path.exists(path):
        store.save(dict(DEFAULT_CONFIG))
    created = {"id": ""}

    def fn(cfg):
        rules = cfg.setdefault("rules", [])
        rid = (args.id or "").strip()
        used = {str(r.get("id", "")) for r in rules}
        if not rid:
            n = 1
            while f"rule-{n}" in used:
                n += 1
            rid = f"rule-{n}"
        if rid in used:
            raise ValueError(f"规则 id '{rid}' 已存在")
        when: dict = {}
        models = _csv(args.model)
        if models:
            when["model"] = models
        for key, val in (("provider", args.provider), ("scenario", args.scenario),
                         ("tags", args.tags), ("protocol", args.protocol)):
            vals = _csv(val)
            if vals:
                when[key] = vals
        if args.has_tools:
            when["has_tools"] = True
        if args.has_image:
            when["has_image"] = True
        if args.min_prompt_tokens is not None:
            when["min_prompt_tokens"] = args.min_prompt_tokens
        if args.max_prompt_tokens is not None:
            when["max_prompt_tokens"] = args.max_prompt_tokens
        if args.prompt_regex:
            try:
                re.compile(args.prompt_regex)
            except re.error as e:
                raise ValueError(f"prompt_regex 不是合法正则：{e}")
            when["prompt_regex"] = args.prompt_regex
        route = {
            "providers": _csv(args.providers) or ["*"],
            "tactic": _tactic(args.tactic),

            "fallback": not args.no_fallback,
        }
        if args.retries is not None:
            route["retries"] = args.retries
        if args.target_model:
            route["model"] = args.target_model
        entry = {"id": rid, "enabled": not args.disabled, "when": when, "route": route}
        if args.description:
            entry["description"] = args.description
        if args.at is not None:
            rules.insert(max(0, min(args.at, len(rules))), entry)
        else:
            rules.append(entry)
        created["id"] = rid

    try:
        store.mutate(fn)
    except ValueError as e:
        print(f"✗ {e}")
        return 2
    cfg = store.snapshot()
    rules = cfg.get("rules") or []
    index = next((i for i, r in enumerate(rules) if str(r.get("id")) == created["id"]), -1)
    print(json.dumps({"ok": True, "id": created["id"], "index": index,
                      "rule": rules[index] if index >= 0 else None, "path": path},
                     ensure_ascii=False))
    return 0


def cmd_rule_remove(args) -> int:
    """按 id 或下标删除一条路由规则。"""
    path = args.config or default_config_path()
    store = ConfigStore(path)
    target = str(args.target).strip()
    removed = {"id": ""}

    def fn(cfg):
        rules = cfg.get("rules") or []
        idx = _rule_index(rules, target)
        if idx is None:
            raise ValueError(f"没有找到路由规则：{target}")
        removed["id"] = str(rules[idx].get("id", ""))
        rules.pop(idx)
        cfg["rules"] = rules

    try:
        store.mutate(fn)
    except ValueError as e:
        print(f"✗ {e}")
        return 2
    print(json.dumps({"ok": True, "removed": removed["id"], "path": path}, ensure_ascii=False))
    return 0


def cmd_rule_move(args) -> int:
    """上移/下移一条路由规则（规则按顺序匹配，越靠前优先级越高）。"""
    path = args.config or default_config_path()
    store = ConfigStore(path)
    target = str(args.target).strip()
    direction = str(args.direction).strip().lower()
    if direction in ("up", "上", "上移"):
        step = -1
    elif direction in ("down", "下", "下移"):
        step = 1
    else:
        print(f"✗ 方向只能是 up/down：{args.direction}")
        return 2
    moved = {"id": "", "index": -1, "moved": False}

    def fn(cfg):
        rules = cfg.get("rules") or []
        idx = _rule_index(rules, target)
        if idx is None:
            raise ValueError(f"没有找到路由规则：{target}")
        new = idx + step
        if 0 <= new < len(rules):
            rules[idx], rules[new] = rules[new], rules[idx]
            moved["moved"] = True
        moved["id"] = str(rules[new if moved["moved"] else idx].get("id", ""))
        moved["index"] = new if moved["moved"] else idx
        cfg["rules"] = rules

    try:
        store.mutate(fn)
    except ValueError as e:
        print(f"✗ {e}")
        return 2
    print(json.dumps({"ok": True, "id": moved["id"], "index": moved["index"],
                      "moved": moved["moved"], "path": path}, ensure_ascii=False))
    return 0


def cmd_config(args) -> int:
    path = args.config or default_config_path()
    store = ConfigStore(path)
    cfg = store.snapshot()
    redacted = json.loads(json.dumps(cfg))
    for p in redacted.get("providers") or []:
        p["has_key"] = bool(p.get("api_key"))    # 打码前记下是否已配置密钥，供面板显示
        if p.get("api_key"):
            p["api_key"] = "***"
        for k in list((p.get("headers") or {}).keys()):
            if any(t in k.lower() for t in ("auth", "key", "token")):
                p["headers"][k] = "***"
        # 内部字段不下发；把 _missing_env 暴露成面板/本地应用用的 missing_env
        miss = p.pop("_missing_env", None) or []
        for k in [k for k in list(p.keys()) if k.startswith("_")]:
            p.pop(k, None)
        p["missing_env"] = miss
    srv = redacted.get("server")
    if isinstance(srv, dict) and srv.get("api_key"):
        srv["api_key"] = "***"
    if args.json:
        print(json.dumps(redacted, ensure_ascii=False, indent=2))
        return 0
    print(f"# {path}")
    print(json.dumps(redacted, ensure_ascii=False, indent=2))
    if store.errors():
        print("配置告警：")
        for e in store.errors():
            print("  ⚠", e)
    return 0


def cmd_port(args) -> int:
    print(read_int(port_file(args)))
    return 0


def cmd_doctor(args) -> int:
    ok = True
    print(f"llm-hub v{VERSION}   python {sys.version.split()[0]}  ({sys.executable})")
    for mod in ("llmhub_canon", "llmhub_adapters", "llmhub_core", "llmhub_server"):
        try:
            __import__(mod)
            print(f"  ✓ 模块 {mod}")
        except Exception as e:  # noqa: BLE001
            ok = False
            print(f"  ✗ 模块 {mod}: {type(e).__name__}: {e}")
    cfg_path = args.config or default_config_path()
    print(f"  配置路径 {cfg_path}  {'存在' if os.path.exists(cfg_path) else '不存在（先 llmhubd init）'}")
    try:
        cfg_dir = os.path.dirname(cfg_path)
        os.makedirs(cfg_dir, exist_ok=True)
        probe = os.path.join(cfg_dir, ".llmhub-write-test")
        with open(probe, "w") as f:
            f.write("x")
        os.remove(probe)
        print("  ✓ 配置目录可写")
    except OSError as e:
        ok = False
        print(f"  ✗ 配置目录不可写：{e}")
    d = data_dir(args)
    try:
        probe = os.path.join(d, ".llmhub-write-test")
        with open(probe, "w") as f:
            f.write("x")
        os.remove(probe)
        print(f"  ✓ 数据目录可写 {d}")
    except OSError as e:
        ok = False
        print(f"  ✗ 数据目录不可写 {d}: {e}")
    print(f"  {'运行中' if pid_alive(read_int(pid_file(args))) else '未运行'}（pid 文件 {pid_file(args)}）")
    try:
        store = ConfigStore(cfg_path)
        cfg = store.snapshot()
        for e in store.errors():
            print("  ⚠", e)
        enabled = [p for p in (cfg.get("providers") or []) if p.get("enabled")]
        print(f"  启用 provider {len(enabled)} 个：" +
              ", ".join(p["id"] for p in enabled) if enabled else "  没有启用任何 provider")
        for p in cfg.get("providers") or []:
            if p.get("_missing_env"):
                print(f"  ⚠ {p['id']} 缺少环境变量：{', '.join(p['_missing_env'])}")
        has_real = [p for p in enabled if p.get("type") != "mock" and p.get("api_key")]
        if not has_real:
            print("  提示：当前只有 mock/无密钥 provider，接入真实模型请编辑配置后 llmhubd reload")
        if not any(p.get("type") == "mock" for p in enabled):
            print("  提示：启用一个 type=mock 的 provider 可以在无网络/无密钥时自检全链路（llmhubd test mock）")
    except Exception as e:  # noqa: BLE001
        ok = False
        print(f"  ✗ 配置解析失败：{e}")
    print("\n结论：" + ("全部就绪 ✓" if ok else "存在问题 ✗（见上）"))
    return 0 if ok else 1


# ── 参数解析 ────────────────────────────────────────────────────────────────
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog=BIN_NAME, description="大模型控制中心 —— 网关 CLI",
                                formatter_class=argparse.RawDescriptionHelpFormatter,
                                epilog=__doc__)
    p.add_argument("--config", help="配置文件路径（默认 $XDG_CONFIG_HOME/llm-hub/config.json）")
    p.add_argument("--data-dir", help="数据目录（默认 $XDG_STATE_HOME/llm-hub）")
    p.add_argument("--version", action="version", version=f"llm-hub {VERSION}")
    sub = p.add_subparsers(dest="cmd")

    def add(name, fn, help_):
        s = sub.add_parser(name, help=help_)
        s.set_defaults(func=fn)
        return s

    s = add("serve", cmd_serve, "前台运行网关")
    s.add_argument("--host")
    s.add_argument("--port", type=int)
    s.add_argument("--quiet", action="store_true", help="不打印请求日志")

    s = add("start", cmd_start, "后台启动网关")
    s.add_argument("--port", type=int)
    s.add_argument("--quiet", action="store_true")
    add("stop", cmd_stop, "停止网关")
    s = add("restart", cmd_restart, "重启网关（先停后启）")
    s.add_argument("--port", type=int)
    s.add_argument("--quiet", action="store_true")
    add("status", cmd_status, "查看状态与 provider 概览")
    add("health", cmd_health, "打印 /health 原始 JSON")
    add("reload", cmd_reload, "重新加载配置文件（热重载）")

    s = add("ask", cmd_ask, "一次性提问（本地最简接口）")
    s.add_argument("prompt", nargs="?")
    s.add_argument("-m", "--model", default="")
    s.add_argument("--provider")
    s.add_argument("--scenario")
    s.add_argument("--system")
    s.add_argument("--max-tokens", type=int, default=1024)
    s.add_argument("--stream", action="store_true", help="SSE 流式输出")
    s.add_argument("--json", action="store_true")
    s.add_argument("-v", "--verbose", action="store_true", help="在 stderr 打印路由信息")

    s = add("chat", cmd_chat, "交互式对话")
    s.add_argument("-m", "--model", default="")
    s.add_argument("--provider")
    s.add_argument("--max-tokens", type=int, default=2048)

    s = add("route", cmd_route, "规则干跑：显示会选哪个 provider（不发请求）")
    s.add_argument("-m", "--model", default="*")
    s.add_argument("-p", "--prompt", default="")

    s = add("models", cmd_models, "列出可用模型（含别名）")
    s.add_argument("--json", action="store_true")
    s = add("providers", cmd_providers, "列出 provider 与路由规则")
    s.add_argument("--json", action="store_true")
    s = add("stats", cmd_stats, "用量统计")
    s.add_argument("--json", action="store_true")
    s = add("logs", cmd_logs, "查看请求日志")
    s.add_argument("-n", "--limit", type=int, default=30)
    s.add_argument("-f", "--follow", action="store_true", help="跟踪守护进程日志文件")
    s.add_argument("--remote", action="store_true", help="走 /logs 接口取结构化请求日志")
    s.add_argument("--json", action="store_true", help="以 JSON 输出（隐含 --remote）")
    add("clear-logs", cmd_clear_logs, "清空请求日志")
    s = add("reset-stats", cmd_reset_stats, "重置统计")
    s.add_argument("--all", action="store_true", help="连同累计值一起清零")

    s = add("enable", lambda a: cmd_toggle(a, True), "启用某 provider")
    s.add_argument("id")
    s = add("disable", lambda a: cmd_toggle(a, False), "停用某 provider")
    s.add_argument("id")
    s = add("test", cmd_test, "测试某 provider 连通性")
    s.add_argument("id")
    s.add_argument("-p", "--prompt", default="ping：请用一句话回答 pong")
    s.add_argument("-m", "--model")
    s.add_argument("--max-tokens", type=int, default=256)   # 推理模型会先花在思维链上
    s.add_argument("--json", action="store_true")

    s = add("init", cmd_init, "写配置模板")
    s.add_argument("--force", action="store_true")
    s = add("set", cmd_set, "修改配置里的一个键（如 server.port）")
    s.add_argument("key", help="点分路径，如 server.api_key / providers.0.enabled")
    s.add_argument("value")
    s.add_argument("--json", action="store_true", help="value 按 JSON 解析（强制类型）；--json null 表示删除该键")

    s = add("provider-add", cmd_provider_add, "新增一个 provider（面板「添加服务商」用）")
    s.add_argument("--id", help="provider id（默认自动取 provider-N）")
    s.add_argument("--type", default="openai", help="openai / anthropic / gemini / ollama / mock")
    s.add_argument("--label")
    s.add_argument("--base-url", help="服务商 API 地址，如 https://api.deepseek.com")
    s.add_argument("--key", help="API key（也可写 ${ENV_VAR} 引用环境变量）")
    s.add_argument("--model", action="append", help="可重复：模型名或 glob 模式")
    s.add_argument("--priority", type=int)
    s.add_argument("--disabled", action="store_true", help="建好后先保持停用")
    s = add("provider-remove", cmd_provider_remove, "按 id 或下标删除 provider")
    s.add_argument("target")
    s = add("rule-add", cmd_rule_add, "新增一条路由规则（面板「添加规则」用）")
    s.add_argument("--id", help="规则 id（默认自动取 rule-N）")
    s.add_argument("--description")
    s.add_argument("--model", action="append", help="匹配请求的模型名，可重复、支持 glob")
    s.add_argument("--provider", action="append", help="匹配请求指定的 provider，可重复")
    s.add_argument("--scenario", action="append", help="匹配场景前缀，可重复")
    s.add_argument("--tags", action="append", help="匹配请求标签（X-LLMHub-Tags），可重复")
    s.add_argument("--protocol", action="append", help="匹配入站协议，可重复")
    s.add_argument("--has-tools", action="store_true", help="只匹配带工具的请求")
    s.add_argument("--has-image", action="store_true", help="只匹配带图片的请求")
    s.add_argument("--min-prompt-tokens", type=int)
    s.add_argument("--max-prompt-tokens", type=int)
    s.add_argument("--prompt-regex", help="对提示词做正则匹配")
    s.add_argument("--providers", help="候选链，逗号分隔（如 deepseek,anthropic;* 表示兜底）")
    s.add_argument("--tactic", help="listed/priority/cheapest/fastest/least_used/random/round_robin")
    s.add_argument("--retries", type=int)
    s.add_argument("--target-model", help="转发给上游时改写成这个模型名")
    s.add_argument("--no-fallback", action="store_true", help="候选链都失败时不走 default_route")
    s.add_argument("--disabled", action="store_true", help="建好后先保持禁用")
    s.add_argument("--at", type=int, help="插入位置（默认追加到末尾）")
    s = add("rule-remove", cmd_rule_remove, "按 id 或下标删除路由规则")
    s.add_argument("target")
    s = add("rule-move", cmd_rule_move, "上移/下移路由规则（顺序即优先级）")
    s.add_argument("target")
    s.add_argument("direction", help="up / down")
    s = add("config", cmd_config, "打印当前配置（密钥打码）")
    s.add_argument("--json", action="store_true")
    add("port", cmd_port, "打印网关端口")
    s = add("doctor", cmd_doctor, "环境自检")
    return p


def _hoist(argv: list[str]) -> list[str]:
    """把全局选项提前到子命令之前，使 `llmhubd serve --config X` 也可用。"""
    head: list[str] = []
    rest: list[str] = []
    i = 0
    seen_ddash = False
    while i < len(argv):
        a = argv[i]
        if a == "--":
            seen_ddash = True
            rest.append(a)
            i += 1
            continue
        if not seen_ddash and a in ("--config", "--data-dir") and i + 1 < len(argv):
            head += [a, argv[i + 1]]
            i += 2
            continue
        if not seen_ddash and (a.startswith("--config=") or a.startswith("--data-dir=")):
            head.append(a)
            i += 1
            continue
        rest.append(a)
        i += 1
    return head + rest


def main(argv: list[str] | None = None) -> int:
    argv = _hoist(list(sys.argv[1:] if argv is None else argv))
    parser = build_parser()
    args = parser.parse_args(argv)
    # 统一成绝对路径：serve 子进程的工作目录与父进程不同，相对路径会被解析到别处
    if getattr(args, "config", None):
        args.config = os.path.abspath(os.path.expanduser(args.config))
    if getattr(args, "data_dir", None):
        args.data_dir = os.path.abspath(os.path.expanduser(args.data_dir))
    if not getattr(args, "cmd", None):
        parser.print_help()
        return 0
    try:
        return args.func(args) or 0
    except SystemExit as e:
        code = e.code
        if code is None:
            return 0
        if isinstance(code, int):
            return code
        sys.stderr.write(f"{code}\n")
        return 1
    except KeyboardInterrupt:
        print()
        return 130


if __name__ == "__main__":
    sys.exit(main())
