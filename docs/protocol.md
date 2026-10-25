# 上游协议契约（ai.cuhk.edu.cn）

抓包确认于 2026-10-25。随时可能因学校改版失效。

## 认证

```
GET  /auth/login
  → 302 sts.cuhk.edu.cn/adfs/oauth2/authorize?...
GET  (authorize 链接)
  → ADFS 密码表单（UserName=cuhksz\<学号> 或邮箱, Password=...）
POST 表单 action（allow_redirects=True）
  → 回跳 ai.cuhk.edu.cn，cookie 即凭证
```

- 无 API key；cookie 就是凭证。TLS 指纹需模拟 Chrome（curl_cffi `impersonate="chrome"`）。
- 会话过期：请求 401/403 → 重走登录流重试一次。

## 聊天

```
POST /chat/
Content-Type: application/json
X-Service: chat-service
```

新对话：

```json
{"approach_id": "glm-5.3-flash", "params": {"thinking_effort": "low"},
 "content": "纯字符串", "parent_idx": -1, "image_ids": [], "file_ids": [],
 "quota_pool": "Students Pool"}
```

续问（同一会话）：

```json
{"chat_session_id": "<start 事件返回的 id>", ..., "parent_idx": <上一条消息的 self_idx>}
```

- `approach_id` 即模型；`parent_idx: -1` 表示新开。
- 响应 `application/x-ndjson+json`，真流式，三种事件：

```
{"event":"start", "chat_session_id":..., "approach_msg_idx":N, "user_msg_idx":M, ...}
{"event":"msg", "item":{"content":"增量文本","type":"text"}, "stream_seq":N}
{"event":"end", "ctx_token_cnt":N, "max_context_length":1000000, "context_truncated":false, "status":"finished"}
```

- 续问的 `parent_idx` 取上一轮 `start` 事件的 `approach_msg_idx`。

## 其他端点

- `POST /getHistoryItem/` `{"chat_session_id": ...}` → 完整消息树（每条带 self_idx/parent_idx）
- `POST /process_ai_detect/?quota_pool=Students Pool` → AIGC 检测（aigcd-arena 先例）

## 限流与配额

- 响应头 `x-ratelimit-limit: 120` / `x-ratelimit-remaining: N`
- 配额耗尽信号：429/402，或 body 含 quota/limit/余额/配额 等关键词
- 模型目录：本地部署版（glm-5.3-flash / qwen3.8-27b，标"不计刷量"）；云端 Claude/豆包/DeepSeek/Gemini/MiniMax/GPT/GLM 各系

## 已知特性

- 服务端垫有 ~8.6k token 隐藏系统脚手架（空消息 ctx_token_cnt 亦为 ~8.6k），协议层不可见、不可去除，会以"校园事务/邮件"类角色设定漏出
- 无原生 system role、无原生 tool call 字段——`content` 为裸字符串
- 模型的注入防御较敏感：注入工具协议时放 content 末尾易被当成攻击拒答，应前置并使用环境配置口吻；"记住暗号"类剧本措辞会触发拒绝
