#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""llmhub_core —— 配置、规则路由、用量统计与执行引擎。

设计要点：
  * 配置是一个 JSON 文件，支持 ${ENV} / ${ENV:-默认值} 引用环境变量；
  * 路由 = 规则表（when 条件）+ 候选 provider 展开（tactic 排序）+ 逐级 fallback；
  * 执行引擎同步实现，任何一次失败都会记录 attempts，并把实际命中的 provider 信息
    交回服务端写入响应头 X-LLMHub-* 与请求日志。
"""
from __future__ import annotations

import fnmatch
import json
import os
import random
import re
import threading
import time
from collections import Counter, deque
from dataclasses import dataclass, field
from typing import Any, Callable, Iterator

from llmhub_canon import CanonRequest, CanonResponse
from llmhub_adapters import (ConfigError, HttpRequest, LineStream, UpstreamError, check_status,
                             get_adapter, http_request)

VERSION = "1.2.7"
CONFIG_VERSION = 1


# ── 路径与基础工具 ──────────────────────────────────────────────────────────


def default_config_path() -> str:
    p = os.environ.get("LLMHUB_CONFIG")
    if p:
        return os.path.expanduser(p)
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(os.path.expanduser("~"), ".config")
    return os.path.join(base, "llm-hub", "config.json")


def default_data_dir() -> str:
    p = os.environ.get("LLMHUB_DATA_DIR")
    if p:
        return os.path.expanduser(p)
    base = os.environ.get("XDG_STATE_HOME") or os.path.join(os.path.expanduser("~"), ".local", "state")
    return os.path.join(base, "llm-hub")


_ENV_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")


def expand_env(value: Any, missing: list[str] | None = None) -> Any:
    """展开 ${VAR} / ${VAR:-默认}；未设置且无默认时记入 missing 并返回空串。"""
    if not isinstance(value, str):
        return value
    if value.startswith("env:"):
        name = value[4:]
        v = os.environ.get(name)
        if v is None:
            if missing is not None:
                missing.append(name)
            return ""
        return v

    def sub(m: re.Match) -> str:
        name, dflt = m.group(1), m.group(2)
        v = os.environ.get(name)
        if v is None:
            if dflt is not None:
                return dflt
            if missing is not None:
                missing.append(name)
            return ""
        return v

    return _ENV_RE.sub(sub, value)


def glob_match(pattern: str, value: str) -> bool:
    if not pattern or pattern == "*":
        return True
    if fnmatch.fnmatchcase(value, pattern):
        return True
    # 允许 "claude-opus-4-*" 这类前缀写法
    return pattern.endswith("*") and value.startswith(pattern[:-1])


def match_any(patterns: Any, value: str) -> bool:
    if patterns is None:
        return True
    if isinstance(patterns, str):
        patterns = [patterns]
    if not patterns:
        return True
    return any(glob_match(str(p), value) for p in patterns)


# ── 默认配置 ────────────────────────────────────────────────────────────────

DEFAULT_CONFIG: dict = {
    "config_version": CONFIG_VERSION,
    "server": {
        "host": "localhost",        # localhost = 127.0.0.1 + ::1；可写逗号分隔的多个地址
        "port": 8787,
        "api_key": "",
        "request_timeout_ms": 600000,
        "log_requests": True,
        "max_log_entries": 500,
    },
    "guardrails": {
        "max_prompt_chars": 0,
        "block_patterns": [],
        "mask_response": [],
    },
    "providers": [
        {
            "id": "mock",
            "label": "内置模拟（无需 key）",
            "type": "mock",
            "enabled": True,
            "models": ["mock-echo", "*"],
            "priority": 1,
            "mock": {"reply": "你好，我是内置模拟模型。", "echo": True, "latency_ms": 30},
        },
        {
            "id": "deepseek",
            "label": "DeepSeek 官方",
            "type": "openai",
            "enabled": False,
            "base_url": "https://api.deepseek.com/v1",
            "api_key": "${DEEPSEEK_API_KEY}",
            "models": ["deepseek-chat", "deepseek-reasoner"],
            "priority": 10,
            "price": {"in": 0.27, "out": 1.10},
        },
        {
            "id": "openai",
            "label": "OpenAI",
            "type": "openai",
            "enabled": False,
            "base_url": "https://api.openai.com/v1",
            "api_key": "${OPENAI_API_KEY}",
            "models": ["gpt-4o", "gpt-4o-mini", "o3-mini"],
            "priority": 20,
            "price": {"in": 2.50, "out": 10.00},
        },
        {
            "id": "anthropic",
            "label": "Anthropic Claude",
            "type": "anthropic",
            "enabled": False,
            "base_url": "https://api.anthropic.com",
            "api_key": "${ANTHROPIC_API_KEY}",
            "models": ["claude-sonnet-4-*", "claude-opus-4-*"],
            "priority": 30,
        },
        {
            "id": "ollama",
            "label": "本地 Ollama",
            "type": "ollama",
            "enabled": False,
            "base_url": "http://127.0.0.1:11434",
            "api_key": "",
            "models": ["*"],
            "priority": 5,
            "price": {"in": 0, "out": 0},
        },
    ],
    "rules": [
        {
            "id": "cheap-background",
            "when": {"model": ["*-mini", "*-nano", "mock-*"], "has_tools": False},
            "route": {"providers": ["mock", "*"], "tactic": "cheapest", "fallback": True},
        },
        {
            "id": "long-context",
            "when": {"min_prompt_tokens": 60000},
            "route": {"providers": ["*"], "tactic": "priority", "fallback": True},
        },
    ],
    "default_route": {"providers": ["*"], "tactic": "priority", "fallback": True, "retries": 0},
    "aliases": {},
    "scenarios": {},
}


def deep_merge(base: Any, extra: Any) -> Any:
    if isinstance(base, dict) and isinstance(extra, dict):
        out = dict(base)
        for k, v in extra.items():
            out[k] = deep_merge(base[k], v) if k in base else v
        return out
    return extra


DEFAULT_PROVIDER_TEMPLATES = {
    "openai": {"base_url": "https://api.openai.com/v1", "path": "/v1/chat/completions"},
    "anthropic": {"base_url": "https://api.anthropic.com", "path": "/v1/messages"},
    "gemini": {"base_url": "https://generativelanguage.googleapis.com", "path": ""},
    "ollama": {"base_url": "http://127.0.0.1:11434", "path": "/api/chat"},
    "mock": {"base_url": "mock://local", "path": ""},
}


# ── 配置加载 ────────────────────────────────────────────────────────────────


class ConfigError2(ConfigError):
    pass


class ConfigStore:
    """带 mtime 热重载与线程安全快照的配置文件读取器。"""

    def __init__(self, path: str):
        self.path = path
        self._lock = threading.RLock()
        self._cfg: dict = {}
        self._mtime: float = 0.0
        self._errors: list[str] = []
        self.reload(force=True)

    # -- 读写 --
    def reload(self, force: bool = False) -> bool:
        try:
            mtime = os.path.getmtime(self.path)
        except OSError:
            if force:
                with self._lock:
                    self._cfg = self._normalize(dict(DEFAULT_CONFIG))
                    self._errors = [f"配置文件不存在，使用内置默认配置: {self.path}"]
            return False
        if not force and mtime == self._mtime:
            return False
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                raw = json.load(f)
        except Exception as e:
            with self._lock:
                self._errors = [f"配置解析失败（保留旧配置）: {e}"]
            return False
        with self._lock:
            self._cfg = self._normalize(raw)
            self._mtime = mtime
            self._errors = []
        return True

    def maybe_reload(self) -> bool:
        return self.reload(force=False)

    def snapshot(self) -> dict:
        with self._lock:
            return self._cfg

    def errors(self) -> list[str]:
        with self._lock:
            return list(self._errors)

    @staticmethod
    def _strip_internal(cfg: dict) -> dict:
        """写盘前去掉以 _ 开头的内部字段（_missing_env 等），别污染用户配置。"""
        out = dict(cfg)
        provs = []
        for p in out.get("providers") or []:
            if isinstance(p, dict):
                provs.append({k: v for k, v in p.items() if not str(k).startswith("_")})
            else:
                provs.append(p)
        out["providers"] = provs
        return out

    def save(self, cfg: dict) -> None:
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        tmp = self.path + ".tmp"
        data = self._strip_internal(cfg)
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.write("\n")
        try:
            os.chmod(tmp, 0o600)   # 配置里可能有 api_key：只给属主读写
        except OSError:
            pass
        os.replace(tmp, self.path)
        with self._lock:
            self._cfg = self._normalize(cfg)
            try:
                self._mtime = os.path.getmtime(self.path)
            except OSError:
                self._mtime = 0.0
            self._errors = []

    def mutate(self, fn: Callable[[dict], dict | None]) -> dict:
        """读-改-写配置文件（用于面板改 provider / 开关）。"""
        cfg = json.loads(json.dumps(self.snapshot(), ensure_ascii=False))
        out = fn(cfg)
        if out is not None:
            cfg = out
        self.save(cfg)
        return cfg

    # -- 归一化 --
    def _normalize(self, raw: dict) -> dict:
        cfg = deep_merge(DEFAULT_CONFIG, raw or {})
        cfg["providers"] = [self._norm_provider(p) for p in (cfg.get("providers") or [])
                            if isinstance(p, dict) and p.get("id")]
        cfg["rules"] = [dict(r, id=str(r.get("id") or f"rule{i}"))
                        for i, r in enumerate(cfg.get("rules") or []) if isinstance(r, dict)]
        cfg["scenarios"] = {str(k): v for k, v in (cfg.get("scenarios") or {}).items()
                            if isinstance(v, dict)}
        cfg["aliases"] = {str(k): str(v) for k, v in (cfg.get("aliases") or {}).items()}
        return cfg

    @staticmethod
    def _norm_provider(p: dict) -> dict:
        missing: list[str] = []
        out = dict(p)
        out["id"] = str(p.get("id"))
        out["label"] = str(p.get("label") or p.get("id"))
        out["type"] = str(p.get("type") or "openai").lower()
        out["enabled"] = bool(p.get("enabled", True))
        tmpl = DEFAULT_PROVIDER_TEMPLATES.get(out["type"], {})
        out["base_url"] = expand_env(p.get("base_url") or tmpl.get("base_url") or "", missing)
        out["path"] = expand_env(p.get("path", tmpl.get("path", "")), missing)
        out["api_key"] = expand_env(p.get("api_key") or "", missing)
        out["headers"] = {str(k): expand_env(v, missing) for k, v in (p.get("headers") or {}).items()}
        raw_models = p.get("models")
        # 显式空列表 = 还没配模型（面板新建的 provider）：保持为空，让它不参与路由
        if raw_models is None:
            models = ["*"]
        elif isinstance(raw_models, list):
            models = raw_models
        else:
            models = [raw_models]
        out["models"] = [str(m) for m in models]
        out["model_map"] = {str(k): str(v) for k, v in (p.get("model_map") or {}).items()}
        out["priority"] = int(p.get("priority", 100))
        out["timeout_ms"] = int(p.get("timeout_ms") or 60000)
        out["tags"] = [str(t) for t in (p.get("tags") or [])]
        out["params"] = dict(p.get("params") or {})
        out["price"] = dict(p.get("price") or {})
        out["auth"] = dict(p.get("auth") or {})
        out["rpm"] = int(p.get("rpm") or 0)
        out["mock"] = dict(p.get("mock") or {})
        out["_missing_env"] = sorted(set(missing))
        return out


# ── 用量统计与请求日志 ──────────────────────────────────────────────────────


@dataclass
class ProviderStat:
    requests: int = 0
    ok: int = 0
    errors: int = 0
    fallbacks: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    cost: float = 0.0
    latency_ms_ewma: float = 0.0
    last_error: str = ""
    last_error_at: float = 0.0
    last_used_at: float = 0.0
    by_model: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "requests": self.requests, "ok": self.ok, "errors": self.errors,
            "fallbacks": self.fallbacks, "tokens_in": self.tokens_in,
            "tokens_out": self.tokens_out, "cost": round(self.cost, 6),
            "latency_ms": round(self.latency_ms_ewma, 1),
            "last_error": self.last_error, "last_error_at": self.last_error_at,
            "last_used_at": self.last_used_at, "by_model": self.by_model,
        }


class Stats:
    def __init__(self, data_dir: str, log_limit: int = 500):
        self.data_dir = data_dir
        self.lock = threading.RLock()
        self.started_at = time.time()
        self.providers: dict[str, ProviderStat] = {}
        self.by_model: Counter = Counter()
        self.by_rule: Counter = Counter()
        self.total_requests = 0
        self.total_ok = 0
        self.total_errors = 0
        self.total_tokens_in = 0
        self.total_tokens_out = 0
        self.total_cost = 0.0
        self.logs: deque = deque(maxlen=max(50, int(log_limit)))
        self._dirty = 0
        self._load()

    # -- 持久化 --
    def _state_file(self) -> str:
        return os.path.join(self.data_dir, "stats.json")

    def _load(self):
        try:
            with open(self._state_file(), "r", encoding="utf-8") as f:
                d = json.load(f)
            self.total_requests = int(d.get("total_requests") or 0)
            self.total_ok = int(d.get("total_ok") or 0)
            self.total_errors = int(d.get("total_errors") or 0)
            self.total_tokens_in = int(d.get("total_tokens_in") or 0)
            self.total_tokens_out = int(d.get("total_tokens_out") or 0)
            self.total_cost = float(d.get("total_cost") or 0)
            for pid, s in (d.get("providers") or {}).items():
                st = ProviderStat(**{k: v for k, v in s.items() if k in ProviderStat.__annotations__})
                self.providers[pid] = st
        except Exception:
            pass

    def flush(self, force: bool = False):
        with self.lock:
            now = time.time()
            if not force and now - self._dirty < 15:
                return
            self._dirty = now
            payload = {
                "updated_at": now,
                "total_requests": self.total_requests, "total_ok": self.total_ok,
                "total_errors": self.total_errors,
                "total_tokens_in": self.total_tokens_in, "total_tokens_out": self.total_tokens_out,
                "total_cost": self.total_cost,
                "providers": {k: v.as_dict() for k, v in self.providers.items()},
            }
        try:
            os.makedirs(self.data_dir, exist_ok=True)
            tmp = self._state_file() + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=1)
            os.replace(tmp, self._state_file())
        except Exception:
            pass

    # -- 记录 --
    def get(self, pid: str) -> ProviderStat:
        with self.lock:
            st = self.providers.get(pid)
            if st is None:
                st = ProviderStat()
                self.providers[pid] = st
            return st

    def count(self, pid: str) -> int:
        with self.lock:
            return self.providers.get(pid, ProviderStat()).requests

    def begin(self, pid: str):
        with self.lock:
            self.total_requests += 1
            st = self.get(pid)
            st.requests += 1
            st.last_used_at = time.time()

    def finish(self, pid: str, ok: bool, ms: float, tokens_in: int = 0, tokens_out: int = 0,
               cost: float = 0.0, model: str = "", error: str = "", fallback: bool = False,
               rule: str = ""):
        with self.lock:
            st = self.get(pid)
            if ok:
                st.ok += 1
                self.total_ok += 1
                st.latency_ms_ewma = (ms if st.latency_ms_ewma <= 0
                                      else st.latency_ms_ewma * 0.7 + ms * 0.3)
            else:
                st.errors += 1
                self.total_errors += 1
                if fallback:
                    st.fallbacks += 1
                st.last_error = error[:400]
                st.last_error_at = time.time()
            if tokens_in or tokens_out:
                st.tokens_in += tokens_in
                st.tokens_out += tokens_out
                self.total_tokens_in += tokens_in
                self.total_tokens_out += tokens_out
            if cost:
                st.cost += cost
                self.total_cost += cost
            if model and ok:
                st.by_model[model] = st.by_model.get(model, 0) + 1
                self.by_model[model] = self.by_model.get(model, 0) + 1
            if rule:
                self.by_rule[rule] = self.by_rule.get(rule, 0) + 1
        self.flush()

    def add_log(self, entry: dict):
        with self.lock:
            self.logs.append(entry)

    def logs_list(self, limit: int = 100) -> list[dict]:
        with self.lock:
            items = list(self.logs)
        return items[-max(1, limit):][::-1]

    def clear_logs(self):
        with self.lock:
            self.logs.clear()

    def reset(self, keep_cumulative: bool = True):
        with self.lock:
            for st in self.providers.values():
                st.requests = st.ok = st.errors = st.fallbacks = 0
                st.tokens_in = st.tokens_out = 0
                st.cost = 0.0
                st.by_model = {}
            self.logs.clear()
            if not keep_cumulative:
                self.total_requests = self.total_ok = self.total_errors = 0
                self.total_tokens_in = self.total_tokens_out = 0
                self.total_cost = 0.0
        self.flush(force=True)

    def snapshot(self) -> dict:
        with self.lock:
            return {
                "uptime_s": round(time.time() - self.started_at, 1),
                "total_requests": self.total_requests,
                "total_ok": self.total_ok,
                "total_errors": self.total_errors,
                "total_tokens_in": self.total_tokens_in,
                "total_tokens_out": self.total_tokens_out,
                "total_cost": round(self.total_cost, 4),
                "providers": {k: v.as_dict() for k, v in self.providers.items()},
                "by_model": dict(self.by_model.most_common(20)),
                "by_rule": dict(self.by_rule.most_common(20)),
            }


class RateLimiter:
    """按 provider 的 rpm 做滑动窗口限速；超限按 rate_limit 错误处理（触发 fallback）。"""

    def __init__(self):
        self.lock = threading.RLock()
        self.hits: dict[str, deque] = {}

    def allow(self, pid: str, rpm: int) -> bool:
        if not rpm or rpm <= 0:
            return True
        now = time.time()
        with self.lock:
            q = self.hits.setdefault(pid, deque())
            while q and now - q[0] > 60:
                q.popleft()
            if len(q) >= rpm:
                return False
            q.append(now)
            return True


# ── 路由 ────────────────────────────────────────────────────────────────────


@dataclass
class Candidate:
    provider_id: str
    model: str
    priority: int = 100
    price_in: float = 0.0
    price_out: float = 0.0
    est_cost: float = 0.0
    latency_ms: float = 0.0
    uses: int = 0

    def as_dict(self) -> dict:
        return {"provider": self.provider_id, "model": self.model, "priority": self.priority,
                "est_cost": round(self.est_cost, 6), "latency_ms": round(self.latency_ms, 1),
                "uses": self.uses}


@dataclass
class RouteDecision:
    rule_id: str = "default"
    scenario: str = ""
    tactic: str = "priority"
    fallback: bool = True
    retries: int = 0
    candidates: list[Candidate] = field(default_factory=list)
    model_override: str = ""
    notes: list[str] = field(default_factory=list)
    alias: str = ""

    def as_dict(self) -> dict:
        return {
            "rule": self.rule_id, "scenario": self.scenario, "tactic": self.tactic,
            "fallback": self.fallback, "retries": self.retries,
            "alias": self.alias, "model_override": self.model_override,
            "candidates": [c.as_dict() for c in self.candidates],
            "notes": self.notes,
        }


class Router:
    def __init__(self, store: ConfigStore, stats: Stats):
        self.store = store
        self.stats = stats
        self.lock = threading.RLock()
        self.rr: Counter = Counter()

    # -- 条件匹配 --
    @staticmethod
    def _rule_matches(when: dict, req: CanonRequest) -> bool:
        if not when:
            return True
        model = req.model or ""
        if not match_any(when.get("model"), model):
            return False
        if when.get("provider") and not match_any(when["provider"], req.hint_provider):
            return False
        if when.get("scenario") and not match_any(when["scenario"], req.hint_scenario):
            return False
        if when.get("protocol") and not match_any(when["protocol"], req.protocol):
            return False
        if "has_tools" in when and bool(when["has_tools"]) != req.has_tools:
            return False
        if "has_image" in when and bool(when["has_image"]) != req.has_image:
            return False
        tags = when.get("tags")
        if tags and not all(t in req.hint_tags for t in tags):
            return False
        approx = req.approx_prompt_tokens
        if when.get("min_prompt_tokens") is not None and approx < int(when["min_prompt_tokens"]):
            return False
        if when.get("max_prompt_tokens") is not None and approx > int(when["max_prompt_tokens"]):
            return False
        pattern = when.get("prompt_regex")
        if pattern:
            try:
                if not re.search(pattern, req.all_text(), re.I | re.S):
                    return False
            except re.error:
                return False
        ppattern = when.get("path_regex")
        if ppattern:
            try:
                if not re.search(ppattern, req.path or ""):
                    return False
            except re.error:
                return False
        if "stream" in when and bool(when["stream"]) != req.stream:
            return False
        return True

    # -- 候选展开 --
    def _providers_by_id(self, cfg: dict) -> dict[str, dict]:
        return {p["id"]: p for p in cfg.get("providers") or []}

    def _expand_spec(self, spec: str, req: CanonRequest, pbyid: dict,
                     default_model: str) -> list[Candidate]:
        out: list[Candidate] = []
        spec = (spec or "").strip()
        if not spec:
            return out
        if spec == "*" or spec.endswith(":*"):
            forced_pid = spec[:-2] if spec.endswith(":*") else ""
            for p in pbyid.values():
                if not p.get("enabled"):
                    continue
                if not p.get("models"):
                    # 没有声明任何模型 = 尚未配置（面板里新加的 provider 就是这样）
                    continue
                if forced_pid and p["id"] != forced_pid:
                    continue
                model = default_model
                if not match_any(p.get("models"), default_model):
                    # provider 不支持请求的模型：仅当它声明了唯一「具体」模型时改用该模型
                    # （纯 glob 模式不能当作模型名发上游）
                    known = [m for m in (p.get("models") or [])
                             if m and m != "*" and not any(ch in m for ch in "*?[")]
                    if len(known) == 1:
                        model = known[0]
                    else:
                        continue
                out.append(self._cand(p, model))
            return out
        pid, _, model = spec.partition(":")
        p = pbyid.get(pid)
        if p is None:
            return out
        if not p.get("enabled"):
            return out
        model = model or default_model
        if model == "*" or not model:
            model = default_model
        model = p.get("model_map", {}).get(model, model)
        return [self._cand(p, model)]

    def _cand(self, p: dict, model: str) -> Candidate:
        price = p.get("price") or {}
        st = self.stats.providers.get(p["id"])
        return Candidate(
            provider_id=p["id"], model=model, priority=int(p.get("priority", 100)),
            price_in=float(price.get("in") or 0), price_out=float(price.get("out") or 0),
            latency_ms=(st.latency_ms_ewma if st else 0.0),
            uses=(st.requests if st else 0),
        )

    def _order(self, cands: list[Candidate], tactic: str, key: str,
               est_in: int, est_out: int) -> list[Candidate]:
        tactic = (tactic or "priority").lower()
        if tactic in ("listed", "order", "declared"):
            # 保持规则里 providers 声明的顺序（逐级 fallback 的常用写法）
            return list(cands)
        if tactic == "cheapest":
            for c in cands:
                c.est_cost = (est_in / 1e6) * c.price_in + (est_out / 1e6) * c.price_out
            return sorted(cands, key=lambda c: (c.est_cost, c.priority))
        if tactic == "fastest":
            return sorted(cands, key=lambda c: (c.latency_ms or 9e9, c.priority))
        if tactic == "least_used":
            return sorted(cands, key=lambda c: (c.uses, c.priority))
        if tactic == "random":
            out = list(cands)
            random.shuffle(out)
            return out
        if tactic == "round_robin":
            if not cands:
                return cands
            with self.lock:
                n = self.rr[key] % len(cands)
                self.rr[key] += 1
            return cands[n:] + cands[:n]
        # priority（默认）
        return sorted(cands, key=lambda c: c.priority)

    # -- 主入口 --
    def resolve(self, req: CanonRequest, cfg: dict | None = None) -> RouteDecision:
        cfg = cfg if cfg is not None else self.store.snapshot()
        pbyid = self._providers_by_id(cfg)
        dec = RouteDecision()

        alias_target = ""
        model = req.model
        if req.model in (cfg.get("aliases") or {}):
            alias_target = str(cfg["aliases"][req.model])
            dec.alias = alias_target
            pid, _, m = alias_target.partition(":")
            if m:
                model = m
            elif pid in pbyid:
                concrete = [x for x in (pbyid[pid].get("models") or [])
                            if x and x != "*" and not any(ch in x for ch in "*?[")]
                if concrete:
                    model = concrete[0]
            req.hint_provider = req.hint_provider or pid

        scenario = req.hint_scenario
        scope_rules: list[dict] = list(cfg.get("rules") or [])
        scope_default: dict = dict(cfg.get("default_route") or {})
        if scenario:
            sc = (cfg.get("scenarios") or {}).get(scenario) or {}
            dec.scenario = scenario
            if sc:
                scope_rules = list(sc.get("rules") or []) + scope_rules
                scope_default = deep_merge(scope_default, sc.get("default_route") or {})

        probe = CanonRequest(**{**req.__dict__, "model": model})
        matched: dict | None = None
        for rule in scope_rules:
            # 面板里可以把一条规则临时关掉（enabled=false）：关掉的规则不参与匹配，
            # 但仍留在配置里，方便随时再打开。
            if rule.get("enabled") is False:
                continue
            if self._rule_matches(rule.get("when") or {}, probe):
                matched = rule
                break
        if matched:
            route_cfg = dict(matched.get("route") or {})
            if not route_cfg:
                # 规则简写：providers/tactic/fallback/retries/model 直接写在规则顶层
                route_cfg = {k: matched[k] for k in
                             ("providers", "tactic", "fallback", "retries", "model")
                             if k in matched}
        else:
            route_cfg = dict(scope_default)
        if matched:
            dec.rule_id = str(matched.get("id") or "rule")
        elif scenario:
            dec.rule_id = f"scenario:{scenario}"
        else:
            dec.rule_id = "default_route"
        dec.tactic = str(route_cfg.get("tactic") or scope_default.get("tactic") or "priority")
        dec.fallback = bool(route_cfg.get("fallback", scope_default.get("fallback", True)))
        dec.retries = int(route_cfg.get("retries", scope_default.get("retries", 0)) or 0)
        model_override = str(route_cfg.get("model") or "")
        if model_override:
            model_override = model_override.replace("{model}", model)
            dec.model_override = model_override

        specs = route_cfg.get("providers")
        if isinstance(specs, str):
            specs = [specs]
        if not specs:
            specs = ["*"]
        if req.hint_provider:
            specs = [s for s in specs if s not in (req.hint_provider, "*")]

        target_model = model_override or model
        seen: set[tuple[str, str]] = set()
        pinned: list[Candidate] = []
        pinned_cands: list[Candidate] = []

        # ① 别名 / 请求头强制的 provider：固定排在最前，不参与 tactic 重排
        if alias_target:
            pinned = self._expand_spec(alias_target, probe, pbyid, target_model)
            if pinned:
                dec.notes.append(f"别名 {req.model} → {alias_target}")
            else:
                dec.notes.append(f"别名 {req.model} → {alias_target} 当前不可用")
            req.hint_provider = ""
        elif req.hint_provider:
            pinned = (self._expand_spec(req.hint_provider, probe, pbyid, target_model)
                      or self._expand_spec(f"{req.hint_provider}:*", probe, pbyid, target_model))
            dec.notes.append(f"请求头强制 provider={req.hint_provider}")

        for c in pinned:
            if (c.provider_id, c.model) in seen:
                continue
            seen.add((c.provider_id, c.model))
            pinned_cands.append(c)

        # ② 规则/默认路由展开的候选，按 tactic 排序后接在固定候选之后
        rest: list[Candidate] = []
        for spec in specs:
            for c in self._expand_spec(str(spec), probe, pbyid, target_model):
                if (c.provider_id, c.model) in seen:
                    continue
                seen.add((c.provider_id, c.model))
                rest.append(c)
        cands = pinned_cands + self._order(rest, dec.tactic, f"{dec.rule_id}:{target_model}",
                                           probe.approx_prompt_tokens,
                                           max(64, req.max_tokens or 512))
        dec.candidates = cands
        if not cands:
            dec.notes.append("没有任何可用 provider（检查 enabled / models 是否匹配）")
        return dec


# ── 执行引擎 ────────────────────────────────────────────────────────────────


@dataclass
class ExecMeta:
    provider: str = ""
    model: str = ""
    rule: str = ""
    tactic: str = ""
    attempts: list[dict] = field(default_factory=list)
    ms: float = 0.0
    tokens_in: int = 0
    tokens_out: int = 0
    cost: float = 0.0

    def as_dict(self) -> dict:
        return {"provider": self.provider, "model": self.model, "rule": self.rule,
                "tactic": self.tactic, "ms": round(self.ms, 1), "tokens_in": self.tokens_in,
                "tokens_out": self.tokens_out, "cost": round(self.cost, 6),
                "attempts": self.attempts}


class Executor:
    def __init__(self, store: ConfigStore, stats: Stats, router: Router):
        self.store = store
        self.stats = stats
        self.router = router
        self.limiter = RateLimiter()

    # -- 预估与守则 --
    def _est_tokens_out(self, req: CanonRequest) -> int:
        return max(64, req.max_tokens or 512)

    def _cost(self, p: dict, tin: int, tout: int) -> float:
        price = p.get("price") or {}
        return (tin / 1e6) * float(price.get("in") or 0) + (tout / 1e6) * float(price.get("out") or 0)

    def apply_guardrails(self, req: CanonRequest, cfg: dict) -> str | None:
        g = cfg.get("guardrails") or {}
        text = req.all_text()
        limit = int(g.get("max_prompt_chars") or 0)
        if limit and len(text) > limit:
            return f"请求内容过长（{len(text)} > {limit} 字符，被 guardrails.max_prompt_chars 拦截）"
        for pat in g.get("block_patterns") or []:
            try:
                if re.search(str(pat), text):
                    return f"请求命中拦截规则 guardrails.block_patterns: {pat}"
            except re.error:
                continue
        return None

    def mask_response(self, resp: CanonResponse, cfg: dict) -> CanonResponse:
        masks = (cfg.get("guardrails") or {}).get("mask_response") or []
        if not masks or not resp.text:
            return resp
        text = resp.text
        for pat in masks:
            try:
                text = re.sub(str(pat), "[已屏蔽]", text)
            except re.error:
                continue
        resp.text = text
        return resp

    # -- provider 取用 --
    def _provider(self, cfg: dict, pid: str) -> dict:
        for p in cfg.get("providers") or []:
            if p["id"] == pid:
                return p
        raise ConfigError(f"provider 不存在: {pid}")

    def _inject_failure(self, p: dict) -> None:
        """mock provider 的失败注入：用于验证 fallback 链路。"""
        if p.get("type") != "mock":
            return
        fail_times = int((p.get("mock") or {}).get("fail_times") or 0)
        if fail_times and self.stats.count(p["id"]) <= fail_times:
            raise UpstreamError(f"mock 注入的失败（第 {self.stats.count(p['id'])} 次请求）",
                                kind="connect")

    def _guard_limits(self, p: dict) -> None:
        if not self.limiter.allow(p["id"], int(p.get("rpm") or 0)):
            raise UpstreamError(f"provider {p['id']} 触发本地限速（rpm={p.get('rpm')}）",
                                kind="rate_limit")

    def _verify_tls(self, p: dict) -> bool:
        return not bool((p.get("params") or {}).get("allow_insecure_tls"))

    # -- 非流式 --
    def complete(self, req: CanonRequest, dec: RouteDecision, cfg: dict) -> tuple[CanonResponse, ExecMeta]:
        meta = ExecMeta(rule=dec.rule_id, tactic=dec.tactic)
        last_err: Exception | None = None
        deadline = time.time() + (int((cfg.get("server") or {}).get("request_timeout_ms")
                                      or 600000) / 1000.0)
        for idx, cand in enumerate(dec.candidates):
            if time.time() > deadline:
                break
            if idx and not dec.fallback:
                break
            attempts = dec.retries + 1 if idx == 0 else 1
            p = self._provider(cfg, cand.provider_id)
            for attempt in range(attempts):
                self.stats.begin(p["id"])
                t0 = time.time()
                try:
                    self._inject_failure(p)
                    self._guard_limits(p)
                    adapter = get_adapter(p["type"])
                    http_req = adapter.build(req, p, cand.model)
                    if http_req.method == "MOCK":
                        resp = adapter.parse(http_req.body or b"", req, p, cand.model)
                    else:
                        status, _hdrs, body = http_request(http_req, self._verify_tls(p))
                        check_status(status, body)
                        resp = adapter.parse(body, req, p, cand.model)
                    ms = (time.time() - t0) * 1000.0
                    if not resp.prompt_tokens:
                        resp.prompt_tokens = req.approx_prompt_tokens
                    cost = self._cost(p, resp.prompt_tokens, resp.completion_tokens)
                    self.stats.finish(p["id"], True, ms, resp.prompt_tokens,
                                      resp.completion_tokens, cost, cand.model,
                                      rule=dec.rule_id)
                    meta.provider = p["id"]
                    meta.model = cand.model
                    meta.ms = ms
                    meta.tokens_in = resp.prompt_tokens
                    meta.tokens_out = resp.completion_tokens
                    meta.cost = cost
                    meta.attempts.append({"provider": p["id"], "model": cand.model, "ok": True,
                                          "ms": round(ms, 1)})
                    resp.model = cand.model
                    return self.mask_response(resp, cfg), meta
                except (UpstreamError, ConfigError) as e:
                    ms = (time.time() - t0) * 1000.0
                    retryable = getattr(e, "retryable", True)
                    self.stats.finish(p["id"], False, ms, error=str(e), rule=dec.rule_id,
                                      fallback=(idx < len(dec.candidates) - 1))
                    meta.attempts.append({"provider": p["id"], "model": cand.model, "ok": False,
                                          "ms": round(ms, 1), "error": str(e),
                                          "kind": getattr(e, "kind", "error")})
                    last_err = e
                    if not retryable or attempt == attempts - 1:
                        break
                except Exception as e:      # noqa: BLE001 - 任何异常都不应打死网关
                    ms = (time.time() - t0) * 1000.0
                    self.stats.finish(p["id"], False, ms, error=f"{type(e).__name__}: {e}",
                                      rule=dec.rule_id)
                    meta.attempts.append({"provider": p["id"], "model": cand.model, "ok": False,
                                          "ms": round(ms, 1), "error": f"{type(e).__name__}: {e}"})
                    last_err = e
                    break
            if not dec.fallback and last_err is not None:
                break
        if last_err is not None:
            raise last_err
        raise UpstreamError("没有可用的 provider 候选（检查配置里的 provider.enabled 与 models）",
                            kind="connect")

    # -- 流式 --
    def stream(self, req: CanonRequest, dec: RouteDecision, cfg: dict,
               on_event: Callable[[dict], None]) -> ExecMeta:
        meta = ExecMeta(rule=dec.rule_id, tactic=dec.tactic)
        last_err: Exception | None = None
        emitted = 0
        for idx, cand in enumerate(dec.candidates):
            if idx and not dec.fallback:
                break
            p = self._provider(cfg, cand.provider_id)
            self.stats.begin(p["id"])
            t0 = time.time()
            stream: LineStream | None = None
            try:
                self._inject_failure(p)
                self._guard_limits(p)
                adapter = get_adapter(p["type"])
                http_req = adapter.build(req, p, cand.model)
                if http_req.method == "MOCK":
                    events: Iterator[dict] = adapter.parse_stream([], req, p, cand.model)
                else:
                    stream = LineStream(http_req, self._verify_tls(p)).open()
                    if not (200 <= stream.status < 300):
                        body = stream.read()
                        check_status(stream.status, body)
                    events = adapter.parse_stream(stream.lines(), req, p, cand.model)
                # 先告诉客户端真正命中的 provider/model（流式响应头需要它），不计入已发送数据
                on_event({"t": "meta", "provider": p["id"], "model": cand.model,
                          "rule": dec.rule_id, "tactic": dec.tactic})
                tin = tout = 0
                done = False
                for ev in events:
                    if ev.get("t") == "usage":
                        tin = ev.get("prompt") or tin
                        tout = ev.get("completion") or tout
                        continue
                    if ev.get("t") == "done":
                        done = True
                    emitted += 1
                    on_event(ev)
                    if ev.get("t") == "done":
                        break
                if not done and emitted == 0:
                    raise UpstreamError("上游流为空", kind="error")
                ms = (time.time() - t0) * 1000.0
                cost = self._cost(p, tin, tout)
                self.stats.finish(p["id"], True, ms, tin, tout, cost, cand.model,
                                  rule=dec.rule_id)
                meta.provider = p["id"]
                meta.model = cand.model
                meta.ms = ms
                meta.tokens_in = tin
                meta.tokens_out = tout
                meta.cost = cost
                meta.attempts.append({"provider": p["id"], "model": cand.model, "ok": True,
                                      "ms": round(ms, 1)})
                return meta
            except (UpstreamError, ConfigError) as e:
                ms = (time.time() - t0) * 1000.0
                self.stats.finish(p["id"], False, ms, error=str(e), rule=dec.rule_id,
                                  fallback=(emitted == 0 and idx < len(dec.candidates) - 1))
                meta.attempts.append({"provider": p["id"], "model": cand.model, "ok": False,
                                      "ms": round(ms, 1), "error": str(e),
                                      "kind": getattr(e, "kind", "error")})
                last_err = e
                if emitted:        # 已经吐给客户端数据，无法再换 provider
                    raise
                if not getattr(e, "retryable", True):
                    raise
            except Exception as e:  # noqa: BLE001
                ms = (time.time() - t0) * 1000.0
                self.stats.finish(p["id"], False, ms, error=f"{type(e).__name__}: {e}",
                                  rule=dec.rule_id)
                meta.attempts.append({"provider": p["id"], "model": cand.model, "ok": False,
                                      "ms": round(ms, 1), "error": f"{type(e).__name__}: {e}"})
                last_err = e
                if emitted:
                    raise
            finally:
                if stream is not None:
                    stream.close()
        if last_err is not None:
            raise last_err
        raise UpstreamError("没有可用的 provider 候选（检查配置里的 provider.enabled 与 models）",
                            kind="connect")

    # -- 干跑（不真正请求上游） --
    def dry_run(self, req: CanonRequest, dec: RouteDecision, cfg: dict) -> dict:
        pbyid = {p["id"]: p for p in cfg.get("providers") or []}
        est_out = self._est_tokens_out(req)
        est_in = req.approx_prompt_tokens
        cands = []
        for c in dec.candidates:
            p = pbyid.get(c.provider_id) or {}
            cands.append({
                **c.as_dict(),
                "type": p.get("type", ""),
                "enabled": bool(p.get("enabled")),
                "est_tokens_in": est_in,
                "est_tokens_out": est_out,
                "est_cost": round((est_in / 1e6) * c.price_in + (est_out / 1e6) * c.price_out, 6),
                "missing_env": p.get("_missing_env") or [],
                "rpm": p.get("rpm", 0),
            })
        return {**dec.as_dict(), "request": {"model": req.model, "protocol": req.protocol,
                                             "stream": req.stream, "has_tools": req.has_tools,
                                             "has_image": req.has_image,
                                             "approx_prompt_tokens": est_in,
                                             "path": req.path},
                "candidates": cands}


# ── 模型目录 ────────────────────────────────────────────────────────────────


def model_catalog(cfg: dict, stats: Stats) -> list[dict]:
    """汇总所有 provider 声明的模型，并标注哪个会命中路由。"""
    seen: dict[str, dict] = {}
    for p in cfg.get("providers") or []:
        st = stats.providers.get(p["id"])
        for m in p.get("models") or ["*"]:
            if m == "*":
                continue
            entry = seen.setdefault(m, {"id": m, "object": "model", "owned_by": p["id"],
                                        "providers": [], "enabled": False})
            entry["providers"].append(p["id"])
            entry["enabled"] = entry["enabled"] or bool(p.get("enabled"))
            if st and st.requests:
                entry.setdefault("stats", {})[p["id"]] = {
                    "requests": st.requests, "errors": st.errors,
                    "latency_ms": round(st.latency_ms_ewma, 1)}
    return sorted(seen.values(), key=lambda x: x["id"])
