# cuhksz-ai-router

把香港中文大学（深圳）AI 服务平台（ai.cuhk.edu.cn）的网页大模型，逆向成本地 OpenAI 兼容 API 的**单文件路由器**。任何说 OpenAI 协议的客户端（openai SDK / Codex / Claude Code 经转换层 / dsh / curl）指过来就能用。

```text
你的 harness / agent / 脚本
        │  base_url=http://127.0.0.1:8350/v1  api_key=任意值
        ▼
cuhksz-ai-router（本机 127.0.0.1:8350）
        │  ADFS 直连 · 会话续问 · emulated 工具协议 · 真流式
        ▼
ai.cuhk.edu.cn  ←─ 配额记账在你自己的 Students Pool
```

## ⚠️ 先读这个

- 这是**对你自己学生账号的自动化使用**，大概率违反学校服务条款。风险自担，账号级后果（限流/封禁）自己承担。
- 传播请保持克制：源码级小范围分享即可，**不要做公开安装包/宣传**。用的人越多，所有使用者共同承担的风控风险越大。
- 凭据（`config.yaml`）只落本机、已 gitignore、只发往 `sts.cuhk.edu.cn`，代码永不打印、不入日志。

## 功能

- OpenAI 兼容：`GET /v1/models` · `POST /v1/chat/completions`（stream 可选）
- 直连无浏览器：ADFS 表单登录 + cookie 会话，过期自动重登
- 服务端会话续问：`X-Session-Id` 头（任意本地凭证）→ 上游 `chat_session_id` 映射，多轮只投增量
- compact 自愈：客户端压缩/改写历史时前缀校验自动分叉新会话，路由器重启丢表同样自愈
- emulated 工具调用：`tools` 参数翻译为受限文本协议，模型输出解析回标准 `tool_calls`（多格式容错）
- 运行日志：`runtime/router.ndjson` 每请求一行元数据（绝不记内容）

## 模型列表

12 个模型，全部经网页实测抓取 approach_id（无统一命名规律，别自己猜，以本表为准）：

| model 名（配置填这个） | 网页展示名 | approach_id（上游真实 id） | 配额 |
|---|---|---|---|
| `glm-5.3-flash-local` | GLM 5.3 Flash（本地版） | `glm-5.3-flash` | **不计刷量** |
| `qwen3.8-27b-local` | Qwen3.8 27B（本地版） | `qwen3.8-27b` | **不计刷量** |
| `claude-haiku-5-5` | Claude Haiku 5.5 | `claude-haiku-5-5` | 计量 |
| `doubao-seed-evolving` | 豆包 Seed Evolving | `doubao-seed-evolving` | 计量 |
| `doubao-seed-2-1-pro` | 豆包 Seed 2.1 Pro | `doubao-seed-2-1-pro-260915` | 计量 |
| `doubao-seed-2-1-turbo` | 豆包 Seed 2.1 Turbo | `doubao-seed-2-1-turbo-260628` | 计量 |
| `deepseek-v4-pro` | DeepSeek V4 Pro | `deepseek-v4-pro` | 计量 |
| `deepseek-flash` | DeepSeek V4.1 Flash | `deepseek-flash` | 计量 |
| `gemini-3.8-flash` | Gemini 3.8 Flash | `gemini-3.8-flash` | 计量 |
| `gemini-3.5-flash-lite` | Gemini 3.5 Flash-Lite | `gemini-3.5-flash-lite` | 计量 |
| `minimax-m3` | MiniMax M3 | `minimax-m3` | 计量 |
| `gpt-6-luna` | GPT 6 Luna | `gpt-6-luna` | 计量 |

> **请优先使用前两个本地部署模型**（名字带 `-local`）。它们跑在学校本地集群上，
> **不消耗你的 Students Pool 云端配额**——给 agent 高频调用、日常问答、子代理任务
> 都应该用它们；云端计量模型留给确实需要更强能力的关键任务。限流为 120 请求/周期，
> 多 harness 共用时省着点用。
>
> 命名无规律可循（点号有时保留有时变横线、豆包带内部日期后缀、DeepSeek V4.1 Flash
> 的 id 干脆叫 `deepseek-flash`）。学校改版后若某模型报 400，重抓 approach_id 即可，
> 方法见 [docs/protocol.md](docs/protocol.md)。

## 安装（Linux / Windows / macOS 通用）

依赖：Python 3.10+ 与 [uv](https://docs.astral.sh/uv/)（或自行用任意 venv 管理）。

```bash
git clone https://github.com/Josh-Zhangccc/cuhksz-ai-router.git
cd cuhksz-ai-router
uv venv .venv
uv pip install --python .venv/bin/python fastapi uvicorn curl_cffi beautifulsoup4 pyyaml
# Windows: .venv\Scripts\python；macOS/linux: .venv/bin/python（下同）
```

## 登录与启动

```bash
.venv/bin/python gateway.py login     # 输入学号/密码，验证登录，写入 config.yaml (0600)
.venv/bin/python gateway.py           # 启动，监听 127.0.0.1:8350
.venv/bin/python gateway.py --debug   # 排障模式：完整请求/响应落 runtime/debug/（含 prompt，勿外传）
```

- 仅适用**纯密码 SSO** 账号；开了 MFA 的账号此路不通（无解，别试）。
- **Windows 注意**：`0600` 权限在 NTFS 上映射不全，多人共用机器上 `config.yaml` 可能被其他本地账户读取。

## 接入示例

```python
from openai import OpenAI
client = OpenAI(base_url="http://127.0.0.1:8350/v1", api_key="sk-anything")

# 普通对话
client.chat.completions.create(
    model="glm-5.3-flash-local",
    messages=[{"role": "user", "content": "你好"}])

# 持续会话：同一 X-Session-Id 的请求共享上游上下文
client.chat.completions.create(..., extra_headers={"X-Session-Id": "my-agent"})

# 工具调用：标准 tools 参数即可，工具执行仍在你的 harness 侧
client.chat.completions.create(..., tools=[...])
```

### dsh（DeepSeek Harness）

`llm-pi-ai` 自定义路由三要素（patch overlay 或 profile 配置）：

```yaml
cuhksz:
  baseURL: http://127.0.0.1:8350/v1
  apiKeyEnv: CUHK_GATEWAY_KEY
  api: openai-completions
  models:
    - id: glm-5.3-flash-local   # 优先：本地部署，不计配额
    - id: qwen3.8-27b-local     # 优先：本地部署，不计配额
```

### 局限（如实说）

- 上游协议无原生 system role / tool call：system 以 `[系统指令]` 前置模拟（优先级弱于真 system）；工具走 emulated 文本协议，模型偶发格式漂移（解析器已多格式容错）或拒绝（安全脚手架误判注入，换措辞可解）
- 上游单次 content 限 ~10 万字符：超大工具集（如 ZCode 全量 33 工具 ~150K）新会话自动分轮投递（定义分片入库、末轮带真实对话，零信息损失），代价是每新会话多 1-2 次上游请求；续问轮不重复投递，不受影响
- 服务端自带 ~8.6k token 隐藏脚手架，不可去除
- 工具轮流式为攒完转发（协议性限制）；限流 120 请求/周期，多 harness 共用消耗加速

## 协议细节

抓包确认的上游契约见 [docs/protocol.md](docs/protocol.md)。

## 致谢与边界

设计思路参考 [hfp556/cuhk-webAI-gateway](https://github.com/hfp556/cuhk-webAI-gateway)（MIT）的公开文档——该项目走浏览器自动化路线，本项目为纯 HTTP 直连路线，代码互不依赖。软件许可证不授予学校 AI 服务、账号或模型任何使用权。

## License

MIT
