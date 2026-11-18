# businessapi.com.au → MCP Server

把 Business API（澳大利亚 ASIC / ATO / ABRS / PPSR 的数字服务商）包装成 MCP 服务器，
让 Claude 等大模型"连上就知道怎么用"，并且能安全地完成公司注册/改名/注销、ABN 申请、商号注册/续期、
ASIC 484/361/362/RA52 表单、实体监控、webhooks 管理等真实操作。

## 先说结论

- 官方 **没有公开 OpenAPI/Swagger**，只有 HTML 文档（`https://businessapi.com.au/developers/api/<name>`，共 13 页 + 端点总表 + webhooks 页 + ASIC/ATO 代码表）。
  所以不能一键自动生成；这里把文档"翻译"成了 Pydantic 模型 + 工具描述 + 资源文档。
- 接口特点：REST + JSON；`Authorization: Bearer bapi_sk_{test|live}_...`；
  沙箱 `https://businessapi.com.au/api/test/v2/`，生产 `https://businessapi.com.au/api/v2/`；
  **几乎所有 POST 都是异步"递交"**（返回 `requestId`，再轮询状态端点或收 webhook），生产环境按次收费。
- 模型"知道怎么用"靠的是 MCP 握手时拿到的三样东西：
  1. 服务器 `instructions`（连上时的一段总述：环境、异步、收费、dry_run 流程、先读哪个资源、如何取 schema）
  2. 每个 tool 的 **名字 + 描述 + 参数 schema**。递交工具的请求体由 Pydantic 模型校验，字段名与官方文档逐字一致，
     字段说明、枚举（ASIC/ATO 代码表）、"Mandatory IF…" 条件规则都在 schema/校验器里
  3. **resources**（`bapi://docs/*` 12 份文档 + `bapi://schemas/{form}` 9 份请求 schema）

## 文件

| 文件 | 作用 |
|---|---|
| `server.py` | 路线 A：手写 MCP。45 个工具 + 12 个文档资源 + 1 个 schema 资源模板 + 1 个 workflow prompt |
| `models.py` | 递交请求的 Pydantic 模型（Form 201 / ABNREG / BNREG / BNRENEW / 484 / 205A / 6010 / 361 / 362 / RA52 / 监控实体）+ ABN/ACN/TFN 校验和 + 代码表 + webhook URL 校验 |
| `tools_list_size.py` | 测量不同配置下 `tools/list` 的上下文开销 |
| `openapi.yaml` | 路线 B：OpenAPI 3.1，由 `export_openapi.py` 从 `models.py` 生成递交端点 schema，其余手写 |
| `server_from_openapi.py` | 路线 B：`FastMCP.from_openapi()` 把 YAML 里每个 `operationId` 自动变成工具 |
| `export_openapi.py` | 把 `models.py` 的 JSON Schema、监控和 webhooks 端点合并进 `openapi.yaml`（改完模型后重跑） |
| `.mcp.json.example` | 注册到 Claude Code / Claude Desktop / Cursor 的配置样例（单服务器 + 按业务拆分两种） |

## 上下文瘦身：两步取 schema + 按业务拆分

十个递交工具的完整 schema 如果内联进 `tools/list`，每次连接都要吃掉约 14 万字符（≈3.6 万 token）。
现在默认是 **懒加载模式**：`lodge_*` 工具的 `request` 参数只是"一个 JSON 对象"，模型需要时调
`get_request_schema(form)` 或读 `bapi://schemas/{form}` 拿到精确 schema；提交时服务器仍用同一套 Pydantic 模型校验，
错误以可读列表返回（例如 `generalInformation.abn: '123' is not a valid ABN`）。

| 配置 | 工具数 | 字符 | ≈token |
|---|---|---|---|
| `BAPI_INLINE_SCHEMAS=true`，全部工具 | 45 | 144,480 | 36,120 |
| 默认（懒加载），全部工具 | 45 | 50,289 | 12,572 |
| `BAPI_TOOLSETS=lookup,registrations,core` | 17 | 18,266 | 4,566 |
| `BAPI_TOOLSETS=agent_forms,core` | 15 | 17,674 | 4,418 |
| `BAPI_TOOLSETS=monitoring,webhooks` | 16 | 17,012 | 4,253 |
| `BAPI_TOOLSETS=lookup,ppsr` | 7 | 6,579 | 1,644 |

（`uv run --with fastmcp tools_list_size.py` 可重跑；token 按 4 字符估算。）

- `BAPI_INLINE_SCHEMAS=true`：切回内联完整 schema，适合会按 schema 渲染表单的客户端，或者只装一两个递交工具的场景。
- `BAPI_TOOLSETS=a,b`：只注册这些分组，一份代码按业务跑成多个小服务器。分组：
  `lookup`（免费查询）、`registrations`（公司/ABN/商号注册与续期）、`agent_forms`（484/205A/6010/361/362/RA52）、
  `monitoring`、`webhooks`、`ppsr`、`core`（状态轮询、账户、`call_endpoint`）。`get_request_schema` 随 `registrations`/`agent_forms` 出现。
  未知分组名启动即报错。每个工具也带同名 tag（`_meta.fastmcp.tags`），支持 tag 的客户端可以再过滤。
- 文档资源不受分组影响（资源列表只是 URI 和名字，内容按需读取）。

## 工具一览（server.py，45 个）

| 类别 | 工具 | 说明 |
|---|---|---|
| 免费查询 `lookup` | `lookup_abn` `lookup_acn` | ABN/ACN 校验 + 查询 |
| | `check_company_name_availability` | 公司名可用性 |
| | `check_business_name_availability(name, holder_name)` | 商号可用性；带 `holder_name` 才会返回 `availableForHolder` |
| | `list_business_industries` | 行业代码 |
| 注册递交 `registrations` | `lodge_company_registration(request, dry_run=True)` | ASIC Form 201，约 140 个字段 |
| | `lodge_abn_registration(request, dry_run=True)` | ATO ABN 申请，5 种实体类型 |
| | `lodge_business_name_registration(request, dry_run=True)` | ASIC 商号注册，5 种持有人类型 |
| | `lodge_business_name_renewal` `get_business_name_renewal_status` | 商号续期 1/3 年 |
| | `get_business_name_registration_status` `refresh_business_name_payment_link` | 商号付款链接 / 续发链接 |
| | `get_request_schema(form)` | 取任一 `lodge_*` 的请求 schema（也随 `agent_forms` 出现） |
| 代理表单 `agent_forms` | `lodge_asic_484_form` `get_asic_484_form_status` | 公司变更：地址、高管、股份发行/注销、UHC、优惠费申报、附件 |
| | `lodge_company_name_change` `get_company_name_change_status` | Form 205A 公司改名 |
| | `lodge_company_deregistration` `get_company_deregistration_status` | Form 6010 自愿注销（六项 s.601AA 声明必须全真） |
| | `lodge_asic_361_form` `lodge_asic_362_form` `get_asic_form_status(form, id)` | 代理停止代理 / 公司委任或解除代理 |
| | `request_asic_account_transactions(start, end, …)` | RA52 ASIC 账户交易报表 |
| 实体监控 `monitoring` | `start_entity_monitoring(entities, idempotency_key, dry_run=True)` `renew_entity_monitoring` | 下单/续费 ≤25 个实体，$18/实体/年 |
| | `list_monitored_entities` `get_monitored_entity` `stop_entity_monitoring` `resume_entity_monitoring` | 列表/详情/停止/恢复 |
| | `get_entity_snapshot` `list_monitoring_events` `get_monitoring_event` `simulate_monitoring_event` | 快照、变更事件、沙箱模拟 |
| Webhooks `webhooks` | `list_webhooks` `create_webhook` `update_webhook` `delete_webhook` `rotate_webhook_secret` | 端点管理（secret 只在创建/轮换时返回一次） |
| | `verify_webhook_signature(secret, header, raw_body)` | 本地校验 `X-Bapi-Signature`（HMAC-SHA256），不联网 |
| PPSR `ppsr` | `create_ppsr_search` `get_ppsr_search` | $6/次 |
| 核心 `core` | `get_request_status` `wait_for_request(kind, timeout)` | 轮询到终态，`kind` 覆盖 10 种状态端点 |
| | `get_account_status` `call_endpoint(method, path, …)` | 账户状态；长尾端点兜底 |

资源：`bapi://docs/overview` `endpoints` `company-registrations` `abn-registrations` `business-name-registrations`（含续期）
`asic-484-forms` `asic-agent-forms`（205A/6010/361/362/RA52）`entity-monitoring` `webhooks` `ppsr-searches` `asic-codes` `ato-codes`；
`bapi://schemas/{form}`，form ∈ company_registration, abn_registration, business_name_registration, business_name_renewal,
asic_484_form, company_name_change, company_deregistration, asic_361_form, asic_362_form。

## 快速开始

```bash
# 1. 到 https://businessapi.com.au/register 注册，在 /user/api-keys 生成测试密钥（bapi_sk_test_...，沙箱免费）
# 2. 运行（需要 uv；fastmcp 会自动装）
set BAPI_API_KEY=bapi_sk_test_xxx     # PowerShell: $env:BAPI_API_KEY="bapi_sk_test_xxx"
uv run --with fastmcp server.py
```

依赖说明：代码按 fastmcp 4.x 写（它自带 `httpx2`，所以文件里是 `import httpx2 as httpx`）。
路线 B 多一个 `pyyaml`：`uv run --with fastmcp --with pyyaml server_from_openapi.py`。

注册到 Claude Code（项目级 `.mcp.json`，照 `.mcp.json.example` 改路径和密钥），或者一条命令：

```bash
claude mcp add bapi -e BAPI_API_KEY=bapi_sk_test_xxx -e BAPI_ENV=test -- uv run --with fastmcp C:/path/to/bapi-mcp/server.py
```

拆成多个服务器时，`.mcp.json.example` 里的 `bapi-registrations` / `bapi-monitoring` 两段就是模板，只差 `BAPI_TOOLSETS`。
Claude Desktop：把对应段放进 `claude_desktop_config.json`。

验证：在 Claude 里说"帮我在沙箱里注册一家叫 ACME TRADING PTY LTD 的公司"，它应当先调 `check_company_name_availability`，
读 `bapi://docs/company-registrations`，调 `get_request_schema("company_registration")`，向你收集字段后以 `dry_run=true`
调 `lodge_company_registration` 给你看 payload。

## 环境变量

| 变量 | 默认 | 作用 |
|---|---|---|
| `BAPI_API_KEY` | 必填 | `bapi_sk_test_…` 或 `bapi_sk_live_…` |
| `BAPI_ENV` | `test` | `test` / `live`，必须与密钥前缀一致 |
| `BAPI_ALLOW_LIVE` | 否 | 为 `true` 才允许生产环境的 POST/PATCH/DELETE |
| `BAPI_INLINE_SCHEMAS` | 否 | 为 `true` 时递交工具内联完整 schema |
| `BAPI_TOOLSETS` | `all` | 逗号分隔的分组名，只注册这些工具 |

## 安全设计（别跳过）

- 默认 `BAPI_ENV=test`。密钥前缀与环境不匹配直接报错。
- 生产环境的任何 POST/PATCH/DELETE 都会在发请求前被拒绝，除非显式 `BAPI_ALLOW_LIVE=true`——因为那是真的向 ASIC/ATO 递表并扣费
  （公司注册约 $50 + ASIC 费、ABN 约 $25、商号约 $15 + ASIC 费、PPSR 查询 $6、实体监控 $18/实体/年、注册代理表单按套餐计费）。
- 所有 `lodge_*` 和监控下单/续费工具 **默认 `dry_run=true`**：只做本地校验并返回将要发送的 JSON，模型必须显式传 `dry_run=false` 才真正递交。
  这给了"先给用户看 payload 和费用、确认后再发"的天然断点。公司注销工具额外标了 `destructiveHint`。
- 懒加载模式下校验不缩水：同一套 Pydantic 模型在服务器端跑，错误按字段路径列出。
- 监控下单/续费必须由调用方给出 `idempotency_key`（官方续费接口强制要求），重试同一个 key 不会重复扣费。
- `testTransmissionIndicator` 由服务器按环境注入，模型传了也会被忽略，防止在沙箱里误发真实递交。
- `simulate_monitoring_event` 在生产环境直接拒绝；webhook URL 本地先校验 https + 公网主机。
- 个人 PPSR 查询需要用户确认有 PPS Act 授权目的（`authorised_purpose` 参数）。
- TFN（税号）只做本地校验和检查，工具说明里要求模型不要把 TFN 复述给用户；webhook secret 只出现一次，工具说明要求立刻交给用户保存。
- 工具都带 MCP `annotations`（`readOnlyHint` / `destructiveHint`）和分组 tag，支持这些提示的客户端会据此决定是否弹确认。
- 按业务拆分服务器时，不给监控/webhook 服务器带 `core` 分组，就没有 `call_endpoint` 兜底，权限面更小。

## 本地校验覆盖了什么（models.py）

- 公司注册：类型/类别/子类合法组合（ASIC Table 0003）、LMGT 与股份结构互斥、股份类别唯一且按字母序、
  注册办公室占用人规则、公众公司开放时间、董事 18+、人名禁用词、申请人个人/组织二选一、签署日期不晚于今天。
- ABN：实体类型↔子类型配对、个体经营者规则（不能申请 TFN、要 TFN 或住址、无关联人）、
  公司需 ACN（027/032）、关联人最少数量、`positionHeld` 按子类型白名单、至多一个 POF、TFN 唯一、
  日期窗口（1999 后、至多提前 6 个月）、营业地址禁止 PO Box、`taxRegistrations` 键白名单、SMSF 两个必填块。
- 商号注册：持有人类型对应的必填块（IND / IB 组织+ACN / PTSH 合伙人 / USTR 受托人 / JV 关联人各自 ABN）、
  JV 以外必须有 ABN、IB/PTSH/USTR 必须有 subType、年限 1 或 3。商号续期：ABN 必填、年限 1 或 3、声明为 true。
- Form 484：至少一个变更段；股份发行/注销必须同时带 `shareStructure` 和 `members`；`members.shareClass` 必须存在；
  ASIC V455 合计校验；签名日期在过去 6 个月内；所有日期不在未来；高管 18+；字符串发送前统一大写。
- 公司改名：`proposedName` 与 `acnNameIndicator` 互斥规则、名称预留块、会议日期不在未来、付款方式 DDEB/INV。
- 公司注销：六项 s.601AA 声明必须全部为 true；申请人个人/组织二选一；受通知人地址；签署日期。
- 361/362/RA52：ACN 校验和、362 type 只能 A/C、361 的 `agentAuthorisedIndicator` 与 `declaration` 必须为 true、RA52 日期区间合法且不在未来。
- 监控：每个实体必须有 ACN 和/或 ABN（校验和）、reference ≤100、每批 ≤25、idempotency_key 1–255 字符。
- Webhooks：URL 必须 https 且不是 localhost/私网/链路本地/CGNAT（Python 把 TEST-NET 文档网段也算私网）；事件类型枚举去重。
- 通用：ABN/ACN/TFN 校验和；空字符串、null、空对象/数组会被自动剔除（官方样例里大量用 "" 占位）。

## 验证记录（2026-11-18）

- 官方文档里能解析的全部样例 payload 通过模型校验：公司注册 1、ABN 5 种实体、商号注册 5 种持有人、商号续期 1、Form 484 1、
  公司改名 1、公司注销 1、361 1、362 1、监控下单 1（RA52 官方样例不是合法 JSON，用手写样例）。
  官方样例里有三处明显占位错误已在测试中修正：484/6010/361/362 的 ACN `123456789` 不满足校验和；6010 的 `dateSigned` 写成 `20213006`；
  RA52 的 `includePriorOutstandingIndicator` 写成 `"123456789"`。
- 14 个故意写错的反例全部被拦下并给出可读错误；懒加载模式下错误以字段路径列表返回，例如一个请求同时报出
  `generalInformation.abn`、`generalInformation.term`、`applicant.individual.givenNames`、`applicant.declaresTrueAndCorrectIndicator` 四条。
- 懒加载和内联两种模式下，三套回归脚本结果一致；`BAPI_TOOLSETS=monitoring,webhooks` 只剩 16 个工具且没有 `call_endpoint`；
  未知分组名启动报错；`get_request_schema` 与 `bapi://schemas/{form}` 返回带 `$defs` 的 JSON Schema。
- 用假密钥真实请求沙箱（含带 `Idempotency-Key` 头的监控下单、webhook 创建、484/RA52/注销状态轮询）均返回 `401 Invalid API key.`；
  `wait_for_request` 遇到 HTTP 错误立即停止。
- 以 `BAPI_ENV=live` + 假生产密钥运行：监控停止/续费/模拟、商号续期、webhook 创建/删除/轮换、RA52 等写操作全部在发请求前被拒绝。
- **尚未用真实沙箱密钥跑过端到端递交**。官方文档有几处需要在沙箱里确认：
  金额字段"末两位为分"（100 = $1.00）与样例 `100 股 / totalAmountPaid 100` 不符；
  地址 `country` 规则说"2 字符"但样例写 `NEW ZEALAND`；
  484 页面对 `officeHolders.status`、`members.holdingStatus` 代码的解释与 ASIC Table 0006/0028 不一致（模型里按 ASIC 表注释）；
  鉴权页说 V2 忽略 `testTransmissionIndicator`，而 484/ABN/商号页说它必填（服务器统一注入，两种说法下都安全）。

## 没有公开文档页的端点（只能走 `call_endpoint`）

`/company-name-reservations`（410B）、`/company-name-reservation-extensions`（410F）、`/asic/extracts`、`/tax/registrations`（TAXREG）、
`/asic-370|492|902|ra17|ra50|ra51|ra61|ra63|ra65|ra67|ra71-forms`、`/asic-inbox`、`/asic-inbox/{id}/pdf`。
`bapi://docs/asic-agent-forms` 里有这份清单；请求体结构需要向厂商索取或下载他们的 Postman 集合。

## 怎么扩展

- 加递交端点：在 `models.py` 建请求模型，在 `server.py` 写一个 `_lodge_xxx(request: Model, dry_run)` 实现函数，
  往 `LODGEMENTS` 注册表加一行（key、工具名、分组、模型、实现），懒加载/内联/分组/`get_request_schema` 自动跟上。
- 加普通工具：用 `@tool("<分组>", annotations=...)` 装饰，而不是 `@mcp.tool`。
- 路线 B：改完 `models.py` 跑 `uv run --with fastmcp --with pyyaml export_openapi.py`，`openapi.yaml` 会同步；
  其他端点直接手写进 YAML，`server_from_openapi.py` 不用改。路线 B 没有环境注入、live 写保护和懒加载，仅适合沙箱或只读。
- 生产部署：改成 HTTP 传输（`mcp.run(transport="http", port=8000)`），API key 留在服务端，不下发给客户端；
  递交状态和监控变更改用 webhooks（`verify_webhook_signature` 的逻辑搬进接收端）而不是轮询。
