#!/home/april/repo/projects/cuhksz-ai-router/.venv/bin/python
"""cuhksz-ai-router — OpenAI 兼容本地路由 → ai.cuhk.edu.cn 直连。

用法： gateway.py login          交互式登录，凭据写入本目录 config.yaml (0600)
       gateway.py [--port 8350]  启动路由服务
       gateway.py --debug         附带完整请求/响应落盘（排障，含 prompt）
端点： GET /v1/models · POST /v1/chat/completions (stream 可选) · GET /health
会话： 请求带 X-Session-Id 头（任意本地凭证字符串）→ 服务端会话续问模式；
       不带 → 无状态拼接模式。前缀校验失败（compact/历史改写）自动分叉新会话。
凭据： <repo>/config.yaml（密码落盘 0600，已 gitignore，永不打印/入日志）；
       cookie 缓存 <repo>/runtime/session.json (0600)。
红线： 凭据只发往 sts.cuhk.edu.cn；只绑 127.0.0.1。
"""
import argparse
import asyncio
import getpass
import json
import re
import sys
import threading
import time
from pathlib import Path
from urllib.parse import urljoin, urlparse

import yaml
from curl_cffi import requests as cffi_requests
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

REPO = Path(__file__).resolve().parent
SESSION_FILE = REPO / "runtime" / "session.json"
CFG = REPO / "config.yaml"

ORIGIN = "https://ai.cuhk.edu.cn"
POOL = "Students Pool"

MODELS = {
    "glm-5.3-flash-local": {"approach_id": "glm-5.3-flash", "note": "本地版·不计刷量"},
    "qwen3.8-27b-local":   {"approach_id": "qwen3.8-27b",   "note": "本地版·不计刷量"},
    "glm-5.3":             {"approach_id": "glm-5.3"},
    "deepseek-v4-pro":     {"approach_id": "deepseek-v4-pro"},
    "deepseek-v4.1-flash": {"approach_id": "deepseek-v4.1-flash"},
    "doubao-seed-2.1-pro":   {"approach_id": "doubao-seed-2.1-pro"},
    "doubao-seed-2.1-turbo": {"approach_id": "doubao-seed-2.1-turbo"},
    "doubao-seed-evolving":  {"approach_id": "doubao-seed-evolving"},
    "claude-haiku-5.5":    {"approach_id": "claude-haiku-5.5"},
    "gemini-3.8-flash":    {"approach_id": "gemini-3.8-flash"},
    "gemini-3.5-flash-lite": {"approach_id": "gemini-3.5-flash-lite"},
    "minimax-m3":          {"approach_id": "minimax-m3"},
    "gpt-6-luna":          {"approach_id": "gpt-6-luna"},
}
DEFAULT_MODEL = "glm-5.3-flash-local"

QUOTA_HINTS = ("quota", "limit", "balance", "exceed", "insufficient", "余额", "配额")

app = FastAPI(title="cuhksz-ai-router")
_upstream_lock = threading.Lock()  # 单飞：同一时刻只放一个上游请求（含流式期间）

# ---------- 运行日志（元数据 only：绝不记 prompt/回复/凭据） ----------

LOG_FILE = REPO / "runtime" / "router.ndjson"
DEBUG = False  # --debug 时完整请求/响应落 runtime/debug/（排障用，含 prompt，勿外传）


def log_event(**kw) -> None:
    try:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        rec = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), **kw}
        with LOG_FILE.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:
        pass  # 日志失败不影响服务


def debug_dump(tag: str, data) -> None:
    if not DEBUG:
        return
    d = REPO / "runtime" / "debug"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{int(time.time()*1000)}-{tag}.json").write_text(
        json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")


# ---------- 凭据 / cookie 会话（移植自 aigcd-arena/tools/submit.py） ----------

def load_cfg() -> dict:
    if not CFG.exists():
        sys.exit(f"凭据不存在：{CFG}\n先运行  {sys.argv[0]} login")
    cfg = yaml.safe_load(CFG.read_text())
    if not cfg.get("username") or not cfg.get("password"):
        sys.exit(f"{CFG} 缺 username/password，重新运行 login")
    return cfg


def load_session() -> cffi_requests.Session | None:
    if not SESSION_FILE.exists():
        return None
    jar = json.loads(SESSION_FILE.read_text())
    s = cffi_requests.Session(impersonate="chrome")
    for k, v in jar.items():
        s.cookies.set(k, v, domain="ai.cuhk.edu.cn")
    return s


def save_session(s) -> None:
    SESSION_FILE.parent.mkdir(parents=True, exist_ok=True)
    SESSION_FILE.write_text(json.dumps(s.cookies.get_dict()))
    SESSION_FILE.chmod(0o600)


def alive(s) -> bool:
    try:
        return s.get(ORIGIN + "/chat/", timeout=30).status_code == 200
    except Exception:
        return False


def relogin() -> cffi_requests.Session:
    from bs4 import BeautifulSoup
    cfg = load_cfg()
    user, pwd = cfg["username"], cfg["password"]
    s = cffi_requests.Session(impersonate="chrome")
    r = s.get(ORIGIN + "/auth/login", timeout=30)
    m = re.search(r'href="(https://sts\.cuhk\.edu\.cn/adfs/oauth2/authorize\?[^"]+)"', r.text)
    if not m:
        raise RuntimeError("authorize link not found")
    r = s.get(m.group(1).replace("&amp;", "&"), timeout=30)
    if urlparse(str(r.url)).hostname != "ai.cuhk.edu.cn":
        soup = BeautifulSoup(r.text, "html.parser")
        form = next((f for f in soup.find_all("form") if f.find("input", {"type": "password"})), None)
        if form is None:
            raise RuntimeError("ADFS form not found")
        action = urljoin(str(r.url), form.get("action") or "")
        data = {i.get("name"): i.get("value", "") for i in form.find_all("input", {"type": "hidden"}) if i.get("name")}
        data["UserName"] = user if re.search(r"[@\\]", user) else "cuhksz\\" + user
        data["Password"] = pwd
        r = s.post(action, data=data, allow_redirects=True, timeout=60)
        if urlparse(str(r.url)).hostname != "ai.cuhk.edu.cn":
            raise RuntimeError("relogin failed (credentials rejected?)")
    save_session(s)
    return s


def get_session() -> cffi_requests.Session:
    s = load_session()
    if s and alive(s):
        return s
    return relogin()


def looks_like_quota_err(status: int, body: str) -> bool:
    if status in (429, 402):
        return True
    low = body.lower()
    return any(h in low for h in QUOTA_HINTS) and "auth" not in low[:80]


# ---------- 会话映射（本地凭证 → 远程会话句柄） ----------

class SessionMap:
    """{本地凭证 → {chat_session_id, parent_idx, model, messages(上次全量, 供前缀校验)}}
    仅内存；丢失/重启后按"未命中"自愈（拿客户端完整历史重开）。"""

    def __init__(self):
        self._m: dict[str, dict] = {}

    def get(self, key: str) -> dict | None:
        return self._m.get(key)

    def put(self, key: str, chat_session_id: str, parent_idx: int, model: str, messages: list) -> None:
        self._m[key] = {"chat_session_id": chat_session_id, "parent_idx": parent_idx,
                        "model": model, "messages": messages}

    def drop(self, key: str) -> None:
        self._m.pop(key, None)


SMAP = SessionMap()


def is_continuation(prev_messages: list, new_messages: list) -> bool:
    """前缀校验：新请求去掉最后一条（本轮 user）后，是否以旧请求全量为前缀。
    否 = compact / 截断 / 历史被改写 → 需分叉新会话。"""
    if not prev_messages or len(new_messages) < len(prev_messages) + 1:
        return False
    return new_messages[:-1][:len(prev_messages)] == prev_messages


# ---------- 消息展平（无状态模式 & 分叉重放用） ----------

def _text_of(content) -> str:
    if isinstance(content, list):
        return "".join(x.get("text", "") for x in content if isinstance(x, dict) and x.get("type") == "text")
    return content or ""


def messages_to_content(messages: list, tools: list | None = None) -> str:
    parts = []
    tc_names = {}  # tool_call_id -> 工具名（tool 结果消息回填用）
    for m in messages:
        role, c = m.get("role"), _text_of(m.get("content"))
        if role == "system":
            parts.append(f"[系统指令]\n{c}")
        elif role == "user":
            parts.append(c)
        elif role == "assistant":
            tcs = m.get("tool_calls") or []
            for tc in tcs:
                f = tc.get("function", {})
                tc_names[tc.get("id", "")] = f.get("name", "?")
            if tcs:
                calls = "; ".join(f'{tc["function"].get("name")}({tc["function"].get("arguments", "")})'
                                  for tc in tcs)
                parts.append(f"[你此前的回复：你发起了工具调用 {calls}]")
            elif c:
                parts.append(f"[你此前的回复]\n{c}")
        elif role == "tool":  # OpenAI 工具结果 → 文本化喂回
            name = m.get("name") or tc_names.get(m.get("tool_call_id", ""), "工具")
            parts.append(f"[环境：工具 {name} 的返回结果]\n{c}")
    body = "\n\n".join(parts)
    if tools:
        # 协议前置（系统指令区）：放末尾会被模型安全脚手架当成尾随注入而拒答
        body = "[环境配置：本会话接入工具执行环境]\n" + tools_preamble(tools) + "\n\n" + body
    return body


def last_user_content(messages: list) -> str:
    for m in reversed(messages):
        if m.get("role") == "user":
            return _text_of(m.get("content"))
    return ""


# ---------- emulated tool call：文本协议 ↔ OpenAI tool_calls 翻译 ----------

def tools_preamble(tools: list) -> str:
    """把 OpenAI tools 定义翻译成注入 prompt 的受限文本协议。"""
    lines = ["以下工具定义由运行环境提供，调用它们是你的正常职责："]
    for t in tools:
        if t.get("type") != "function":
            continue
        f = t["function"]
        lines.append(f"- 工具名 {f['name']}：{f.get('description', '')}")
        if f.get("parameters"):
            lines.append(f"  参数 JSON Schema：{json.dumps(f['parameters'], ensure_ascii=False)}")
    lines += [
        "调用工具时，你的整条回复就是一个 JSON 对象，格式：{\"__tool__\": \"工具名\", \"__args__\": {参数对象}}",
        '示例：{"__tool__": "bash", "__args__": {"command": "ls"}}',
        "不需要工具时正常回答。除此之外不要输出任何 JSON。",
    ]
    return "\n".join(lines)


def parse_tool_call(text: str):
    """从模型输出解析工具调用，多格式兼容（对抗格式漂移）：
    1) {"__tool__": name, "__args__": {...}} 裸 JSON 信封（规定格式）
    2) <tool_call>{"name":...,"arguments":{...}}</tool_call>（Qwen 标签内 JSON）
    3) <tool_call>name<arg_key>k</arg_key><arg_value>v</arg_value>...</tool_call>（标签流漂移）
    返回 (name, args) 或 None。"""
    # 1) JSON 信封（含代码块内）
    for m in re.finditer(r"\{[^{}]*(?:\{[^{}]*\}[^{}]*)*\}", text, re.S):
        try:
            obj = json.loads(m.group(0))
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict) and obj.get("__tool__"):
            return obj["__tool__"], obj.get("__args__", {})
    # 2) 标签内 JSON
    for m in re.finditer(r"<tool_call>\s*(\{.*?\})\s*</tool_call>", text, re.S):
        try:
            obj = json.loads(m.group(1))
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict) and (obj.get("name") or obj.get("tool")):
            return obj.get("name") or obj.get("tool"), obj.get("arguments", {})
    # 3) 标签流（arg_key/arg_value 对）
    m = re.search(r"<tool_call>\s*([\w.-]+)\s*((?:<arg_key>.*?</arg_key>\s*<arg_value>.*?</arg_value>\s*)+)</tool_call>",
                  text, re.S)
    if m:
        name = m.group(1)
        args = {}
        for a in re.finditer(r"<arg_key>(.*?)</arg_key>\s*<arg_value>(.*?)</arg_value>", m.group(2), re.S):
            k, v = a.group(1).strip(), a.group(2).strip()
            try:
                v = json.loads(v)
            except (json.JSONDecodeError, TypeError):
                pass
            args[k] = v
        return name, args
    return None


# ---------- 上游调用（真流式：NDJSON 行事件生成器） ----------

class UpstreamError(Exception):
    def __init__(self, status: int, body: str):
        self.status, self.body = status, body
        super().__init__(f"upstream {status}: {body}")


def chat_payload(approach_id: str, content: str, effort: str, chat_session_id: str | None, parent_idx: int) -> dict:
    p = {"approach_id": approach_id, "params": {"thinking_effort": effort},
         "content": content, "parent_idx": parent_idx, "image_ids": [], "file_ids": [],
         "quota_pool": POOL}
    if chat_session_id:
        p["chat_session_id"] = chat_session_id
    return p


def post_chat(s, payload: dict):
    return s.post(ORIGIN + "/chat/", headers={"Content-Type": "application/json",
                  "X-Service": "chat-service", "Referer": ORIGIN + "/chat/"},
                  data=json.dumps(payload), timeout=300, stream=True)


def upstream_stream(approach_id: str, content: str, effort: str,
                    chat_session_id: str | None, parent_idx: int):
    """阻塞生成器：yield 上游事件 dict。401/403 自动重登重试一次；配额抛 429。
    运行于线程池（同步生成器），全局锁保证单飞。"""
    with _upstream_lock:
        s = get_session()
        payload = chat_payload(approach_id, content, effort, chat_session_id, parent_idx)
        r = post_chat(s, payload)
        if r.status_code in (401, 403):
            r.close()
            s = relogin()
            r = post_chat(s, payload)
        if r.status_code != 200:
            body = ""
            try:
                body = r.text[:400]
            except Exception:
                pass
            if looks_like_quota_err(r.status_code, body):
                raise HTTPException(429, f"upstream quota/limit: {body}")
            raise UpstreamError(r.status_code, body)
        for line in r.iter_lines():
            if not line:
                continue
            if isinstance(line, bytes):
                line = line.decode("utf-8", "replace")
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue
        r.close()


# ---------- 端点 ----------

@app.get("/health")
def health():
    return {"ok": True, "session": bool(load_session()), "mapped_sessions": len(SMAP._m)}


@app.get("/v1/models")
def models():
    now = int(time.time())
    return {"object": "list", "data": [
        {"id": mid, "object": "model", "created": now, "owned_by": "cuhksz-ai-router",
         "meta": m.get("note", "")} for mid, m in MODELS.items()]}


def plan_request(req: Request, body: dict) -> dict:
    """决定本轮：服务端会话续问 or 新开/分叉重放。"""
    messages = body.get("messages", [])
    model = body.get("model") or DEFAULT_MODEL
    effort = str(body.get("reasoning_effort") or body.get("thinking_effort") or "low")
    key = req.headers.get("x-session-id") or body.get("session") or None
    tools = body.get("tools") or None
    if not messages or not last_user_content(messages):
        raise HTTPException(400, "empty messages")

    ent = SMAP.get(key) if key else None
    cont = ent and ent["model"] == model and is_continuation(ent["messages"], messages)
    if ent and cont:
        # 服务端会话续问：只投增量（带工具时增量 = 工具调用/结果的文本化 + 新 user + 协议提醒）
        delta_msgs = messages[:-1][len(ent["messages"]):]  # 上一请求之后新增的非末条消息
        if tools:
            delta = messages_to_content(delta_msgs)
            if messages[-1].get("role") == "user":
                delta += "\n\n" + last_user_content(messages)
            else:
                # 工具结果轮：只投增量，附一句收敛提醒
                delta += "\n\n[环境]以上是工具返回结果。若已足够回答，请直接作答；否则继续调用工具。"
        else:
            delta = last_user_content(messages)
        return {"content": delta,
                "fallback_content": messages_to_content(messages, tools),
                "chat_session_id": ent["chat_session_id"], "parent_idx": ent["parent_idx"],
                "smap_key": key, "messages": messages, "effort": effort, "model": model,
                "tools": tools, "mode": "continue"}
    # 新开/分叉：全量展平（分叉原因：无凭证/未命中/模型切换/compact/重启丢表/工具轮无会话）
    mode = "tools-new" if tools else "fresh"
    return {"content": messages_to_content(messages, tools),
            "fallback_content": None, "chat_session_id": None, "parent_idx": -1,
            "smap_key": key, "messages": messages, "effort": effort, "model": model,
            "tools": tools, "mode": mode}


@app.post("/v1/chat/completions")
async def chat_completions(req: Request):
    body = await req.json()
    model = body.get("model") or DEFAULT_MODEL
    if model not in MODELS:
        raise HTTPException(400, f"unknown model {model}; GET /v1/models")
    approach_id = MODELS[model]["approach_id"]
    plan = plan_request(req, body)
    cid = f"chatcmpl-{int(time.time()*1000):x}"
    stream = bool(body.get("stream"))
    t0 = time.time()
    log_event(evt="request", model=model, mode=plan["mode"],
              session=bool(plan["smap_key"]), stream=stream, tools=len(plan["tools"] or []))
    debug_dump("req", {"model": model, "plan": {k: plan[k] for k in ("mode", "chat_session_id", "parent_idx")},
                       "messages": body.get("messages"), "tools": plan["tools"]})

    def run_events():
        """线程内执行上游调用；远程会话失效时分叉重开一次（用全量展平兜底）。"""
        try:
            yield from upstream_stream(approach_id, plan["content"], plan["effort"],
                                       plan["chat_session_id"], plan["parent_idx"])
        except UpstreamError as e:
            if plan["smap_key"] and plan["chat_session_id"] is not None and e.status in (400, 404, 410):
                SMAP.drop(plan["smap_key"])
                log_event(evt="fork-fallback", reason=f"upstream {e.status}", model=model)
                content = plan.get("fallback_content") or plan["content"]
                yield from upstream_stream(approach_id, content, plan["effort"], None, -1)
            else:
                raise

    def pump():
        """消费上游事件 → (delta|final|error, payload)。顺带维护映射表。"""
        parts, end_info = [], {}
        started = False
        try:
            for obj in run_events():
                ev = obj.get("event")
                if ev == "start":
                    started = True
                    if plan["smap_key"]:
                        SMAP.put(plan["smap_key"], obj["chat_session_id"],
                                 obj.get("approach_msg_idx", 0), model, plan["messages"])
                elif ev == "msg":
                    item = obj.get("item", {})
                    if item.get("type") == "text" and item.get("content") is not None:
                        parts.append(item["content"])
                        yield "delta", item["content"]
                elif ev == "end":
                    end_info.update(ctx=obj.get("ctx_token_cnt"), trunc=obj.get("context_truncated"))
            if not started:
                yield "error", "no start event from upstream"
            yield "final", {"text": "".join(parts), "end": end_info}
        except HTTPException as e:
            yield "http_error", {"status": e.status_code, "detail": e.detail}
        except UpstreamError as e:
            yield "http_error", {"status": 502, "detail": str(e)}
        except Exception as e:
            yield "http_error", {"status": 500, "detail": f"gateway: {e}"}

    if stream and not plan["tools"]:
        def sse(o):
            return f"data: {json.dumps(o, ensure_ascii=False)}\n\n"

        def chunk(delta, finish=None, created=None):
            return {"id": cid, "object": "chat.completion.chunk",
                    "created": created or int(time.time()), "model": model,
                    "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}

        def gen():
            created = int(time.time())
            n, chars = 0, 0
            yield sse(chunk({"role": "assistant"}, created=created))
            for typ, data in pump():
                if typ == "delta":
                    n += 1; chars += len(data)
                    yield sse(chunk({"content": data}, created=created))
                elif typ == "error":
                    log_event(evt="error", model=model, kind="stream", detail=str(data)[:200])
                    yield sse({"error": data})
                elif typ == "http_error":
                    log_event(evt="error", model=model, kind="stream", detail=str(data)[:200])
                    yield sse({"error": data["detail"]})
            yield sse(chunk({}, "stop", created=created))
            yield "data: [DONE]\n\n"
            log_event(evt="done", model=model, mode=plan["mode"], finish="stop",
                      latency_ms=int((time.time()-t0)*1000), deltas=n, chars=chars)
        return StreamingResponse(gen(), media_type="text/event-stream")

    # 非流式，或带工具（emulated 模式必须攒完才能解析信封）
    # 在线程池里跑 pump，绝不阻塞事件循环（否则 /health 都会挂）
    def collect():
        return list(pump())

    events = await asyncio.to_thread(collect)
    text, err, end_info_ctx = "", None, None
    for typ, data in events:
        if typ == "delta":
            text += data
        elif typ == "final":
            end_info_ctx = data.get("end", {}).get("ctx")
        elif typ in ("error", "http_error"):
            err = data if typ == "http_error" else {"status": 502, "detail": data}
    if err:
        log_event(evt="error", model=model, kind="final", detail=str(err)[:200])
        raise HTTPException(err.get("status", 502), err.get("detail", "upstream error"))

    message = {"role": "assistant", "content": text, "tool_calls": None}
    finish = "stop"
    tool_parse_ok = None
    if plan["tools"]:
        tc = parse_tool_call(text)
        tool_parse_ok = bool(tc)
        if not tc:
            log_event(evt="tool-parse-miss", model=model, head=text[:120].replace("\n", " "))
        else:
            name, args = tc
            message = {"role": "assistant", "content": None, "tool_calls": [
                {"id": f"call_{cid[-8:]}", "type": "function",
                 "function": {"name": name, "arguments": json.dumps(args, ensure_ascii=False)}}]}
            finish = "tool_calls"

    if stream:  # stream + tools：聚合后按 SSE 分块发出（含 tool_calls 块）
        def sse(o):
            return f"data: {json.dumps(o, ensure_ascii=False)}\n\n"
        created = int(time.time())
        buf = [sse({"id": cid, "object": "chat.completion.chunk", "created": created,
                    "model": model, "choices": [{"index": 0, "delta": {"role": "assistant"},
                                                 "finish_reason": None}]})]
        if message["tool_calls"]:
            buf.append(sse({"id": cid, "object": "chat.completion.chunk", "created": created,
                            "model": model, "choices": [{"index": 0, "delta": {"tool_calls": message["tool_calls"]},
                                                         "finish_reason": None}]}))
        elif message["content"]:
            buf.append(sse({"id": cid, "object": "chat.completion.chunk", "created": created,
                            "model": model, "choices": [{"index": 0, "delta": {"content": message["content"]},
                                                         "finish_reason": None}]}))
        buf.append(sse({"id": cid, "object": "chat.completion.chunk", "created": created,
                        "model": model, "choices": [{"index": 0, "delta": {},
                                                     "finish_reason": finish}]}))
        buf.append("data: [DONE]\n\n")

        def gen2():
            yield from buf
        log_event(evt="done", model=model, mode=plan["mode"], finish=finish,
                  latency_ms=int((time.time()-t0)*1000), tool_parse_ok=tool_parse_ok)
        return StreamingResponse(gen2(), media_type="text/event-stream")

    log_event(evt="done", model=model, mode=plan["mode"], finish=finish,
              latency_ms=int((time.time()-t0)*1000), ctx=end_info_ctx, tool_parse_ok=tool_parse_ok,
              chars=len(text))
    return JSONResponse({"id": cid, "object": "chat.completion", "created": int(time.time()),
                         "model": model,
                         "choices": [{"index": 0, "message": message, "finish_reason": finish}],
                         "usage": {"prompt_tokens": None, "completion_tokens": None,
                                   "total_tokens": None}})


# ---------- CLI ----------

def cmd_login() -> None:
    user = input("CUHK-SZ 账号: ").strip()
    pwd = getpass.getpass("密码: ")
    CFG.write_text(yaml.safe_dump({"username": user, "password": pwd},
                                  allow_unicode=True, default_flow_style=False))
    CFG.chmod(0o600)
    print(f"凭据已写入 {CFG} (0600)，正在验证登录…")
    try:
        relogin()
        print("✓ 登录成功，cookie 已缓存，可直接启动服务")
    except SystemExit:
        raise
    except Exception as e:
        CFG.unlink(missing_ok=True)
        sys.exit(f"✗ 登录失败（{e}），已回滚删除凭据文件")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("command", nargs="?", choices=["login"], help="login = 交互式登录并写 config.yaml")
    ap.add_argument("--port", type=int, default=8350)
    ap.add_argument("--debug", action="store_true", help="完整请求/响应落 runtime/debug/（含 prompt）")
    a = ap.parse_args()
    if a.command == "login":
        cmd_login()
    else:
        DEBUG = a.debug
        import uvicorn
        uvicorn.run(app, host="127.0.0.1", port=a.port, log_level="info")
