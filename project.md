# cuhksz-ai-router

## Goal & Context

把 ai.cuhk.edu.cn 的网页大模型服务（Students Pool）逆向成 OpenAI 兼容的本地 API，
供本机任意 harness / agent 直接以 `base_url + model + api_key` 方式接入。
名字取"本地路由"之义（cuhksz-ai-router）；2026-10-25 由 cuhk-ai-gateway 更名。

- 直连路线：ADFS 表单登录 + cookie 会话 + `POST /chat/` JSON/NDJSON 协议（纯 HTTP，无浏览器）
- 凭据落项目目录 config.yaml（0600 + gitignore），login 子命令写入；永不打印、永不入库
- 会话契约（2026-10-25 抓包确认）：
  - `POST /chat/`，body: `{approach_id, params.thinking_effort, content(纯字符串), parent_idx, image_ids, file_ids, quota_pool:"Students Pool"}`
  - 响应 NDJSON：`start`(带 chat_session_id) → `msg`(item.content) → `end`
  - 限流 `x-ratelimit-limit: 120`；服务端自带 ~8.6k token 隐藏脚手架（不可去除）
  - 本地部署模型（GLM 5.3 Flash 本地版 / Qwen3.8 27B 本地版）不计刷量，优先给 agent 用

## Stages

- [x] S1 逆向侦察：端点/payload/流格式/限流/模型目录 全部确认
- [x] S2 MVP 路由器：/v1/models + /v1/chat/completions（stream + non-stream）
- [x] S3 稳定化：429/配额熔断、会话自动重登、超时与取消、多轮 parent_idx 映射（含真流式 NDJSON→SSE）
- [x] S4a emulated tool loop（2026-10-25）：tools 定义→受限文本协议注入；模型输出 {"__tool__","__args__"} 信封→解析回 OpenAI tool_calls（finish_reason=tool_calls）；tool 结果消息→"[工具 x 返回结果]"文本喂回。dsh headless 真实 bash 工具循环实测通过。边界：工具轮走无状态展平（每轮独立上游会话，成本高）；单轮单工具调用；stream+tools 为攒完转发
- [x] S4b 并发修复（2026-10-25）：同步 pump 曾在 async 处理器内迭代阻塞事件循环（全服务假死，health 挂起），改 asyncio.to_thread 收集，实测工具请求进行中 health 延迟 0.01s
- [x] S4c 工具轮接入服务端会话（2026-10-25）：增量=工具调用/结果文本化+收敛提醒；修两处（assistant tool_calls 拍空串、工具结果轮重复拼旧 user）；确认模型注入防御特性（协议须前置、"暗号"类措辞触发拒答）——沉淀至 docs/protocol.md
- [x] S3b 运行日志（2026-10-25）：runtime/router.ndjson 元数据 only（mode/latency/finish/tool_parse_ok，绝不记内容）+ --debug 完整落盘（runtime/debug/ 含 prompt，勿外传）
- [x] S5 发布（2026-10-25）：README（三平台+警告+局限）+ docs/protocol.md + MIT；GitHub 私有仓 Josh-Zhangccc/cuhksz-ai-router，v0.1
- [ ] S6 待办：/v1/messages 协议面；单轮多工具调用；限流感知退避（x-ratelimit-remaining）；客户端断连传播到上游

## Tasks

- [x] 决策：不复用 hfp556/cuhk-webAI-gateway 代码（浏览器架构与直连路线不兼容），仅借鉴其文档级设计经验（MIT，留署名）
- [x] dsh 接入验证：headless + patch overlay（llm-pi-ai 自定义路由 api: openai-completions + 模型目录）真实对话成功；overlay 存 wiki/tmp/cuhk-gateway.patch.yml
- [x] 多轮对话策略定型（2026-10-25）：X-Session-Id 凭证 → {chat_session_id, parent_idx} 内存映射，续问只投增量；前缀校验失败（compact/历史改写/重启丢表）自动分叉全量重放；无 X-Session-Id 保持无状态拼接模式
- [x] 续问契约抓包（2026-10-25）：续问 = 同端点带 chat_session_id + parent_idx=最后消息 self_idx；/getHistoryItem/ 可查完整消息树；上游为真流式 NDJSON
- [ ] 上游 system 脚手架无法去除——system prompt 以首条 content 前置模拟，效果边界已向用户说明

## Decisions

- 2026-10-25 路线定为 ADFS 直连而非浏览器自动化：aigcd-arena/tools/submit.py 已验证登录流与检测端点，本次抓包补齐聊天端点，浏览器方案（OpenCLI+Chrome）慢且脆，无保留价值。
- 2026-10-25 服务形态定为一个自包含 FastAPI 文件 + 复用 submit.py 会话层，不引入 CCR 等宿主框架：需求只是 OpenAI 兼容端点，重框架维护成本大于收益。
- 2026-10-25 项目更名 cuhksz-ai-router（取本地路由之义），弃用 cuhk-ai-gateway。
- 2026-10-25 凭据落位项目目录 config.yaml（0600 + gitignore），login 子命令交互式写入并验证；不放用户目录——项目小、就地闭环，传播时他人 clone 后 `login` 一步即用。密码必须落盘以支撑无人值守重登，红线：只发往 sts.cuhk.edu.cn、永不打印/入日志/入库。
- 2026-10-25 会话状态全托管上游、路由器仅存映射：远程会话失效/路由器重启均可凭客户端完整历史自愈（分叉重开），本地不备份滚动历史——凭证匹配方案的最薄可靠形态。
