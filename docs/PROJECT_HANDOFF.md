# 多人出行规划 Agent：项目交接说明

> 核对日期：2026-10-12（Asia/Shanghai）
> 核对基线：Phase 4 基础任务已完成，Phase 5 对话闭环及 Agent 优化首版已实现；当前仍有未提交的 Agent 持久化等修改，不等于生产就绪。
> 本文以当前代码、迁移、OpenAPI、Git 状态及实际测试结果为准。`docs_private/` 中部分早期进度描述已经过时，不能单独作为当前状态依据。
> 本轮仅核查并更新本文件，不开发功能、不修改其他文件、不自动提交。既有未提交修改予以保留。

## 1. 当前已经完成的功能

### Phase 1：账号、Trip 与多人协作

- 使用中国大陆 11 位手机号或开发阶段的微信 code 登录；首次登录自动创建用户，昵称可选，已有用户可更新昵称。
- 创建 Trip、通过邀请码加入、列出本人创建/加入的 Trip、查看详情、创建者删除 Trip。
- 创建者自动成为参与者；成员可分别提交出发点、交通方式、最早出发时间和最晚参与时间。
- 创建者与成员权限校验；非成员不能读取 Trip 数据或建立该 Trip 的 WebSocket。
- 智能约点候选、`minimax`/`min_sum` 排序、赞/踩/取消投票和实时广播。
- 登录成功后将开发阶段 token 与用户摘要保存在浏览器本地，刷新会恢复同一账号；不会自动打开旧 Trip，也不会恢复旧地图选点，退出或服务端返回 401 时清除登录态。
- 同一浏览器标签页之间通过 `BroadcastChannel` 同步账号变化；系统仍允许同一账号多端同时登录。

### Phase 2：多人、多起点、多目的地联合路线

- 地点 CRUD、候选/必去/可选/排除状态、地点投票和预计停留时间。
- 单人也可以规划多个地点；多人可以从不同出发点、使用不同交通方式前往第一站集合。
- 创建者设置行程起止时间、共同交通方式以及均衡/总时间/总距离优化目标。
- 支持 2～10 个已确认地点；无固定时间窗时采用最近邻与 2-opt，有固定时间窗时采用时间感知贪心排序。存在必去地点时，第一站候选限制为必去地点，并非枚举所有已确认地点。
- 生成每位参与者到第一站的 ETA、共同路线分段、地点到达/离开时间线、总耗时及警告。
- `available_from` 会推迟个人出发；`available_until` 会产生结构化冲突警告。
- 时间不足时允许跳过 `optional`，但不跳过 `must_visit`。
- 规划快照保存参与者路线和共同路线 geometry，保证不同客户端看到同一结果。
- 输入变更会递增 `input_version`；旧方案标记为过期，过期方案不能确认。
- 创建者可确认最终方案，确认后 Trip 锁定；创建者可以重新打开编辑。
- 地点支持成对设置固定到访时间窗，生成等待及迟到冲突信息；这不是严格可行性求解，详见第 8 节限制。

### Phase 3：地点搜索和分享文本导入

- 城市手动选择、浏览器/高德定位、城市范围 POI 搜索、候选项确认。
- 规则解析结构化文本、标签文本和竖线分隔文本。
- 解析高德直接链接、短链接和 `wb.amap.com/?p=...` 分享参数。
- 可选 LLM 地点提取，支持 OpenAI-compatible Chat Completions 和路径含 `/anthropic` 的 Anthropic Messages 兼容接口。
- 支持用户分别粘贴正文和评论，并标记小红书、大众点评、美团等来源；统一手动导出适配层会清理界面噪声并规范作者/商家回复；评论观点按主题、倾向和证据句汇总，`评论：`/`回复：` 可保留楼层关系；明确出现地点名称的楼层会自动关联到候选地点，用户也可在当前页面手动改绑或取消关联；不自动抓取第三方平台。普通解析/反馈流程不持久化评论原文，但 Agent 导入草案存在原文落库问题，见第 8 节。
- LLM 输出必须通过本地 Pydantic schema；失败时自动回退规则解析。
- 解析结果经过高德地理编码，并要求用户人工确认后才写入 Trip。

### Phase 4：历史反馈与个性化

- Trip 使用 `active → confirmed → completed` 生命周期；已完成行程永久只读，并支持历史状态筛选。
- 已完成 Trip 的成员可编辑自己的访问、评分、实际停留、标签、文字和再访意愿；其他成员只能看到聚合，不能读取他人文字。
- 地点解析接受、修改和拒绝保存严格脱敏快照；不保存输入原文，正式授权流程上线前 `consent_to_improve` 只能为 false。
- 本人可设置显式偏好，并从已完成行程评价重建可解释派生偏好；可随时关闭或清空。
- 至少 3 条有效本人反馈且个性化开启时，候选地点在投票净分上获得最多 0.5 的个人软加分并显示原因；不改变地点原顺序、必去状态或核心路线规划。

### Phase 5：对话式 Agent 与优化首版

- 已实现结构化行程问答、偏好解释、主方案/规划参数修改草案、地点文本解析草案、路线生成草案及受控 `update_destination`（停留、状态、固定时间窗）。页面显示调用来源及 token/cache usage；明确中文指令有窄范围规则兜底。
- 写操作草案绑定 Trip、创建者及输入版本，需用户执行确认；地点仍须逐项人工确认，最终路线确认独立进行。执行结果逐项带 `status`。
- 上下文包含营业/固定时间窗、规划就绪诊断、路线真实/估算摘要、等待与冲突，不发送坐标/geometry。
- `compare_itineraries` 使用共享路线缓存，对相同输入比较 balanced/total_time/distance 三种优化目标，不保存正式方案；尚不是任意假设修改的反事实比较。
- 最近 3 轮原始问答保存在进程内，保留 30 分钟；结构化 action 摘要与可执行草案已迁移 PostgreSQL，支持数据库条件更新领取。该首版仍有隐私、并发和崩溃恢复缺口，不应称为完整生产化或端到端幂等。
- 已有共同约点/多地点界面及地图图层隔离、主方案同步、个人与 Trip 时间回填、评价/停留内联编辑、账号切换表单清理，以及高德 `10021` 进程内熔断与调用统计。

### 前端与运行能力

- 原生 HTML/CSS/JavaScript 单页界面，流程为登录 → 创建/加入/选择 Trip → 选择约点或多地点规划。
- 高德 JS API 用于地图、Marker 和路线展示；未配置或加载失败时使用 Canvas 降级图。
- 高德 JS Key 由服务端 `/api/config` 提供，无需在页面手工填写。
- WebSocket 同步参与者、地点、投票、规划和确认事件。

## 2. 当前目录结构和主要模块职责

```text
project1/
├─ app/
│  ├─ main.py                 FastAPI 生命周期、路由注册、配置接口、静态前端挂载
│  ├─ config.py               .env 配置（数据库、高德、LLM、服务参数）
│  ├─ db.py                   SQLAlchemy async engine/session/Base
│  ├─ models.py               User、Trip、参与者、地点、投票和规划快照 ORM
│  ├─ schemas.py              API 输入校验和业务取值范围
│  ├─ security.py             开发阶段 token 签发与认证
│  ├─ routers/                HTTP 与 WebSocket 路由层
│  └─ services/
│     ├─ access.py            Trip 成员和可编辑状态检查
│     ├─ amap.py              高德 Web 服务、路线/POI/地理编码/分享链接
│     ├─ itinerary.py         联合路线生成、排序、时间线与降级
│     ├─ meeting_point.py     集合点候选及公平性/总成本排序
│     ├─ shared_text.py       确定性分享文本解析
│     ├─ source_adapters.py   跨平台手动导出文本规范化
│     ├─ llm_parser.py        可选 LLM 地点提取适配器
│     ├─ agent_assistant.py   对话上下文、动作识别、诊断与短期原始问答
│     ├─ preferences.py       显式/派生偏好与有界软推荐
│     └─ ws.py                WebSocket 连接管理与广播
├─ frontend/index.html        当前全部前端页面、样式和脚本
├─ migrations/               Alembic 环境与 0001～0009 迁移
├─ tests/test_phase1.py       当前 discover 共 75 个 unittest 单元/规则测试
├─ test_e2e.py               需要已启动服务和 PostgreSQL 的完整 HTTP E2E
├─ scripts/evaluate_place_parser.py  规则/LLM 地点解析评测脚本
├─ scripts/verify_trip_deleted.py     Trip 及 Phase 4/Agent 关联数据级联删除核验
├─ docs_private/              被 Git 忽略的内部设计、记录和评测数据
├─ docs/PROJECT_HANDOFF.md    本交接文档
├─ requirements.txt
├─ alembic.ini
└─ .env.example
```

路由层负责身份、权限、HTTP 语义与事务；路线算法和第三方调用放在 `services/`。坐标在业务对象中使用 `lat/lng`，调用高德时明确转换为 `lng,lat`。

## 3. 数据库和 Alembic 当前状态

- 数据库：PostgreSQL，异步驱动 `asyncpg`；当前不使用 PostGIS，坐标为 Float/JSONB。
- 实际执行 `python -m alembic current` 与 `heads` 均返回：`0009_agent_state (head)`。
- 迁移链：
  - `0001_phase1_baseline`：用户、Trip、参与者、约点结果和投票基础表。
  - `0002_phase2a_destinations`：多目的地、规划参数、`input_version`、`trip_destinations`、`itinerary_plans`。
  - `0003_participant_availability`：参与者 `available_from` / `available_until`。
  - `0004_trip_lifecycle`：`finished → confirmed`，新增 `completed_at` / `completed_by`。
  - `0005_destination_feedback`：已完成行程的逐用户地点评价。
  - `0006_trip_primary_workflow`：创建者控制的 Trip 主方案类型。
  - `0007_place_parse_feedback`：解析候选接受、修改和拒绝的脱敏反馈。
  - `0008_destination_visit_window`：地点级固定到访开始/结束时间及成对约束。
  - `0009_agent_state`：Agent 草案及结构化短期状态；摘要排除 `import_text`，但可执行草案仍保存该字段。过期目前仅限制查询/执行，未实现物理清理。
- 当前主要表：`users`、`trips`、`trip_participants`、`meeting_point_results`、`trip_votes`、`trip_destinations`、`itinerary_plans`、`destination_feedback`、`place_parse_feedback`、`agent_proposals`、`agent_conversation_states`。
- 既有 2026-10-12 完整 E2E 记录为 Trip #36，已删除并核验八张相关表无残留。本轮交接核查没有新建或删除测试 Trip，也没有重跑 HTTP E2E。
- `0009_agent_state.py` 当前为未跟踪文件，但本机数据库已应用；后续交接/提交必须包含该迁移，不能仅交付 ORM 和路由修改。
- Phase 4A-1/2/3 已新增 `completed_at`、`completed_by`、`destination_feedback` 和 `place_parse_feedback`。

迁移操作：

```powershell
conda activate route_plan
python -m alembic current
python -m alembic heads
python -m alembic upgrade head
```

## 4. API 当前实现情况

当前 OpenAPI 实际生成下列业务路径：

| 模块 | 已实现接口 |
|---|---|
| 认证 | `POST /auth/login` |
| Trip | `GET/POST /trips`、`POST /trips/join-by-code`、`POST /trips/{id}/join`、`GET/DELETE /trips/{id}`、`POST /trips/{id}/reopen`、`POST /trips/{id}/complete` |
| 参与者 | `PUT /trips/{id}/participants/me`、`GET /trips/{id}/participants` |
| 地点 | `GET/POST /trips/{id}/destinations`、`PUT/DELETE /trips/{id}/destinations/{destination_id}`、`PUT .../status` |
| 规划 | `GET/PUT /trips/{id}/planning-settings`、`POST /trips/{id}/itinerary-plans`、`GET .../latest`、`POST .../{plan_id}/confirm` |
| 地点发现 | `GET /api/places/search`、`POST /trips/{id}/shared-text/parse` |
| Phase 4 反馈 | `GET /trips/{id}/feedback`、`PUT .../destinations/{destination_id}/feedback/me`、`POST .../place-parse-feedback` |
| 诊断 | `GET /api/places/diagnostics`、`GET /api/diagnostics/llm`（均需登录，不返回 Key/原文） |
| 偏好 | `GET/PUT /users/me/preferences`、`POST .../rebuild`、`DELETE .../derived` |
| 约点/投票 | `GET/POST /trips/{id}/meeting-points`、`GET/POST /trips/{id}/votes` |
| 主方案 | `PUT /trips/{id}/workflow` |
| Agent | `POST /trips/{id}/assistant/chat`、`DELETE .../assistant/history`、`POST .../assistant/proposals/execute` |
| 实时同步 | `WS /trips/{id}/ws?token=...` |
| 基础 | `GET /health`、`GET /api/config`、`/docs`、`/openapi.json` |

注意：当前 FastAPI/Starlette 版本会在 `app.routes` 中显示 `_IncludedRouter`，不能只遍历顶层 `APIRoute` 判断业务路由是否注册；应检查 `app.openapi()["paths"]`。

## 5. 分享文本导入地点功能当前实现情况

处理顺序如下：

1. `services/source_adapters.py` 根据用户选择的来源清理手动粘贴/导出文本，不访问第三方平台网络。
2. `services/shared_text.py` 抽取 URL，并按确定性规则解析标签和分隔文本。
3. 仅允许高德域名做远程分享链接解析，防止任意 URL 抓取；直接 URL 能解析时不发网络请求。
4. 用户勾选 AI 增强且服务端 LLM 可用时，调用 `llm_parser`；LLM 成功后使用其文本候选，避免规则把整句误识别为额外地点，但高德链接候选始终保留。
5. LLM 不可用、失败或响应不合 schema 时回退规则结果，并返回 warning。
6. 非分享链接候选通过高德 POI 搜索或 geocode 补坐标；无法可靠定位时标记为未解析，交给用户地图确认。
7. 最多返回 10 个候选；接口本身不直接创建地点，前端必须由用户逐项确认加入。

已验证两种 LLM 响应格式。私有评测集为 20 条脱敏样例；最近保存的 LLM 报告为 Precision 1.0、Recall 0.9655、F1 0.9825，唯一漏项是纯高德短链，而该场景由确定性短链解析覆盖。评测数据和报告位于 `docs_private/llm_evals/`，不会提交 Git。

## 6. 高德 API 相关实现

- 后端 `AMAP_API_KEY`：Web 服务 Key，用于驾车/公交路线、距离、逆地理编码、地理编码、POI 搜索、周边停车场和分享链接 POI 详情。
- 前端 `AMAP_JS_KEY` 与 `AMAP_JS_SECURITY_CODE`：Web 端 JS API，和后端 Web 服务 Key 不是同一种 Key。
- 当前本机核对结果：后端 Web 服务、JS 地图和 LLM 都处于“已配置”状态；本文档不记录任何实际 Key。
- HTTP 客户端为持久化 `httpx.AsyncClient`，有连接池、并发上限 5、12 秒总超时/5 秒连接超时、一次有限重试，并在应用关闭时释放。
- 公交城市由起点坐标逆地理编码动态解析并做进程内缓存，不再硬编码单一城市。
- 驾车和公交路线会解析耗时、道路距离、polyline 和公交/地铁步骤；路线快照写入规划方案。
- 高德失败、超时、权限不足或未配置时，上层使用直线距离估算辅助排序，并明确标记 `estimated` 与 `fallback_reason`；前端声明其不可作为导航。
- 分享短链最多跟随有限跳转，仅接受高德域名；支持 `surl.amap.com` 和高德 `p` 参数的纬经度顺序差异。

## 7. 已完成的测试

本轮交接核查（2026-10-12）实际执行，使用 `D:\anaconda3\envs\route_plan\python.exe`，未启动新服务：

| 检查 | 结果 |
|---|---|
| `python -m unittest discover -s tests -q` | 75/75 通过（0.150 秒） |
| `python -m compileall -q app tests test_e2e.py scripts` | 通过 |
| 前端内嵌 JavaScript `new Function` 语法检查 | 通过 |
| `python -m alembic current` / `heads` | `0009_agent_state (head)` |
| OpenAPI 路径生成 | 34 个路径正常生成，包含 3 个 Agent 路径 |
| `git diff --check` | 通过；仅有 LF/CRLF 转换提示 |
| HTTP E2E、浏览器验收、付费 LLM/高德请求 | 本轮未执行，不视为本轮通过项 |

75 个单元测试覆盖：成员/非成员访问、锁定 Trip 禁止修改、schema 校验、高德路线与配额熔断、POI/分享链接、单人多地点、Trip 与地点级时间约束、提前等待、必去/可选时间窗冲突、路线快照、约点、投票、规则及评论楼层/回复/地点关联、跨平台手动导出适配、两种 LLM 响应及 token/cache usage、Agent 草案归属/占用/消费、确定性规划诊断、地点修改 action、路线质量降级摘要、短期对话 action 状态、解析反馈隐私字段/授权约束、可解释偏好计算和有界软推荐。Agent 相关测试包含旧内存实现，不能据此推断当前数据库路由已经通过真实并发、重启或崩溃恢复验收。

既有 E2E 覆盖：三用户登录、创建/列出/加入 Trip、非成员 403、两名参与者提交不同出发方式、地点协作与权限、多地点规划与确认/重开、分享文本解析并确认加入、约点、投票/取消、Phase 4 生命周期与完成后只读、评价聚合与文字隐私、解析反馈及授权拒绝、显式/派生偏好软分解释/关闭/清空（包括空数组保存后重新读取），以及测试 Trip 删除级联。上一轮记录中 `scripts/verify_trip_deleted.py` 核验 Trip、参与者、地点、路线、地点反馈、解析反馈及两张 Agent 状态表共八张表均为 0。脚本遇到高德 `10021` 时会跳过真实定位外部断言，因此整体通过不等于所有外部能力均成功。

本轮不重跑 E2E 的原因：脚本使用固定账号，会修改昵称及创建者偏好，结束时未恢复原偏好；Trip 清理也未放入 `finally`。应先完成测试数据隔离和异常清理，再重复执行。上述历史记录不是本轮重新验证的结果。

## 8. 当前已知问题

### 需要优先解决

以下 Agent 问题由本轮静态代码核查发现，尚未通过故障注入复现；本轮只记录，不实施修复：

- P0 隐私：`app/routers/agent.py::_store_proposal` 将完整 action JSON 写入 `agent_proposals.actions`，其中包括 `import_text`。仅 `_save_structured_state` 排除导入原文；不能把摘要脱敏等同于所有 Agent 数据脱敏。草案过期未物理删除，已消费草案也保留内容，需明确最小化保存、保留期限和清理机制。
- P0 执行一致性：领取草案的条件 UPDATE 是原子的，但 `executing` 没有租约/崩溃恢复。路线生成内部提交、执行器提交和标记 consumed 分属不同提交边界；进程中断可能留下已生效动作和未完成草案，外层 rollback 无法撤销已提交的路线。当前没有端到端幂等保证；执行时 Trip 版本检查也没有行锁/CAS 保证。
- P1 多动作丢失：`_store_proposal` 按 `action.type` 去重，同一草案修改多个地点时，多条 `update_destination` 只留下最后一条。应按动作语义/地点标识处理，而不是只按类型去重。
- P1 并发与清空：结构化状态采用先查后插，首次并发请求可能触发 `(trip_id, user_id)` 唯一约束冲突；清空历史没有撤销尚可执行的草案，也不会清除其他 worker 的内存问答。
- P1 多进程部署：WebSocket 房间、原始问答、高德熔断/统计及 token 映射仍是进程内状态。数据库草案领取不代表整个应用支持跨 worker 一致广播、登录或恢复。

1. 2026-09-22 用户已用内置浏览器验证 Phase 4 评价与偏好表单的提交、持久化、回填、校验和取消；两轮检查发现并修复窄窗口地图遮挡控件、刷新丢失登录、确认后摘要状态不同步、偏好保存无反馈、空文本无法清除偏好和登录输入框过窄，修复后的重复页面验收尚待执行。
2. 当前 token 是开发阶段随机 token，持久化于本地 `.token_store.json`；手机号没有所有权验证，微信 code 是开发占位；没有过期、服务端注销/撤销、设备管理或正式微信 code2session。正式认证只做过检查，尚未实现。
3. 默认 `DEBUG=True`，开发模式的未处理 500 会返回 traceback；CORS 也为全开放。不能直接作为公网生产配置。

### 功能与体验限制

- 当前页面是功能验证型原生单页，信息密度、视觉层级、间距、响应式布局和操作反馈尚未形成统一设计系统；下一轮应先做信息架构与交互稿，再拆分单文件前端，避免继续局部打补丁。
- 无固定时间窗时用最近邻 + 2-opt，有固定时间窗时用时间感知贪心，不保证全局最优或必然找到可行顺序；尚未引入 OR-Tools。第一站仅枚举必去地点的策略可能漏掉“可选地点先行”的可行路线。
- 固定时间窗冲突目前主要为警告，并非最终确认的硬性禁止条件；不能承诺所有已确认方案满足时间窗。`total_time` 目标评分没有完整计入等待，不等于全流程耗时全局最小。
- 方案比较只改变三个优化目标；尚未支持“交换 B/C、修改某站时间”等假设场景、与当前方案的差量对比、选择比较结果直接保存及基于计算结果的二次 LLM 解释。比较中的 `total_distance_km` 为共同游览段，不包含各参与者赴约段。
- Agent 尚不能通过全空字段清除已有固定时间窗，也没有个人参与时间修改 action；复杂相对时间指令仍依赖模型理解。
- 地点级固定到访时间窗首版已支持，但目前仅支持单个、最长 24 小时的连续时区时间窗；尚未支持按星期、多段营业时间和跨日复杂规则。
- `available_until` 目前主要生成警告；尚未支持参与者中途离队、分支路线和个人返程。
- 营业时间只支持简单的单日 `HH:MM`，复杂星期规则和跨日营业未完整处理。
- 高德不可用时只能降级估算，不能提供可导航路线；外部 API 的配额、网络和权限仍会影响速度与质量。
- 地址 geocode 可能命中同名地点；虽然已有城市 POI 搜索和人工确认，但公园/景区具体入口仍需用户主动选择。
- `frontend/index.html` 是单文件前端，功能已较多，状态与渲染逻辑维护成本正在上升。
- `GET /trips/{id}` 逐个查询参与者用户，存在 N+1 查询；规模扩大前应改为 join/selectin。
- Trip 删除为永久删除，无归档或回收站；当前 E2E 会清理本次成功运行创建的 Trip，但历史失败运行可能留下测试数据。
- 同一账号可多设备/多页面同时在线，这是当前明确允许的开发行为，不应误判为 bug；正式策略尚未确定。
- 小红书、大众点评和美团当前只支持用户手动粘贴正文/评论，不支持粘贴链接后自动读取平台内容。后续须先验证官方接口、用户授权导出或合规客户端采集方案，不实现绕过登录、验证码或反爬的服务端抓取。

## 9. 尚未完成的任务

- Phase 4A-1 已完成：统一 `active → confirmed → completed` 生命周期，迁移旧 `finished`，增加完成字段/接口、历史筛选和完成后不可变规则。
- Phase 4A-2 已完成：已完成 Trip 的地点访问、评分、实际停留、标签和再访意愿反馈。
- Phase 4A-3 已完成首版：记录地点解析候选被接受/修改/拒绝的脱敏反馈，不保存原始聊天全文；迁移为 `0007_place_parse_feedback`。
- Phase 4B 已完成：基于本人已完成行程反馈生成可解释偏好，支持显式偏好、查看、关闭、重建和清空；候选地点采用最多 0.5 的个人软加分，不改变核心路线排序。
- Phase 5 基础对话闭环及优化首版已实现（见第 1 节），不再是“只有文本提取增强器”。以下带日期的条目是实现沿革，不代表仍需重复开发；尚未完成项以本节末尾清单和第 8、10 节为准。
- Phase 5 基础闭环已完成：支持结构化上下文问答、Phase 4 偏好解释、主方案/规划参数草案、地点文本解析草案和路线生成草案；地点继续经过高德候选与逐项人工确认，最终路线确认仍独立执行。页面展示调用来源与 token。草案绑定 active Trip 的 creator 与输入版本；明确中文指令有窄范围规则兜底。最近 3 轮原始问答仅在进程内保存 30 分钟并可主动清空；这不包含可执行导入草案的存储，其原文落库问题见第 8 节。
- 2026-09-28 Agent 优化第一步：增加草案归属、版本检查和占用/消费状态，执行结果逐项带 `status`。当时的同进程保护不能代替当前数据库执行链的事务与崩溃恢复验收。
- 2026-09-28 Agent 优化第二批已实现：上下文加入地点营业/固定时间窗和脱敏路线摘要，不发送坐标/geometry；API 与页面直接展示确定性 `planning_readiness`、真实/估算路段、等待和固定窗冲突；新增受控 `update_destination` 草案修改停留时长、固定窗和状态，变更后使旧方案过期。短期历史保留不含导入原文的 action 摘要以支持追问。
- 2026-09-28 无落库候选方案比较已完成首版：`compare_itineraries` 使用共享路线缓存模拟 balanced/total_time/distance，返回路线顺序、总耗时、共同段距离、等待、估算路段和时间窗冲突，不写正式方案；E2E 已包含比较后 latest plan 为 404 的断言。
- 2026-09-28 Agent 状态持久化首版已实现：草案与结构化 action 摘要迁移到 PostgreSQL，具备状态及条件领取；未领取且未过期的草案具备重启后读取基础，但执行中崩溃恢复、物理清理和原文最小化尚未完成。迁移为 `0009_agent_state`，Trip 删除核验脚本覆盖两张新表。撤回此前“生产化已完成、不能重复消费”的端到端保证表述，详见第 8 节。
- 2026-10-12 Phase 4 完成审计：Phase 4A-1、4A-2、4A-3、4B 的代码、迁移、前端与数据治理要求均已满足；75/75 单元测试、compileall、前端内嵌 JavaScript、8 条 Phase 4 必需 OpenAPI 路径、Alembic `0009_agent_state (head)`、`git diff --check` 和完整数据库 HTTP E2E 全部通过。E2E Trip #36 已删除，直接查询 Trip、参与者、地点、路线、地点反馈、解析反馈及两张 Agent 状态表均为 0。Phase 4 计划内任务无剩余；修复后浏览器重复视觉验收属于 UI 回归建议，不是 Phase 4 功能缺口。
- 正式认证、账号安全、设备/session 管理、HTTPS、审计、备份、数据导出/删除和云端部署。
- Agent 原文最小化/清理、执行租约与恢复、事务及幂等边界、Trip 并发版本保护、多地点动作保留、状态 upsert、清空历史与草案撤销语义。
- 针对真实 PostgreSQL Agent 路由的并发/重启/失败恢复测试，以及 E2E 独立账号、偏好恢复和 `finally` 清理。
- 任意假设场景的方案差量比较与选定保存、时间窗硬约束策略、Agent 清除时间窗及个人时间修改能力。
- 多段/周期/跨日地点时间窗、参与者提前离队/返程、入口级选点、百度分享链接与坐标系转换。
- 更系统的数据库路由集成测试、前端自动化测试、外部 API mock/契约测试和 CI。

## 10. 下一阶段开发建议

Phase 4 基础任务已闭环；Phase 5 基础闭环不等于所有扩展/生产要求完成。下一阶段先修正第 8 节风险，再扩展功能，不重复开发已有能力：

1. P0：明确 Agent 导入原文保存策略，实施最小化保存与到期清理；梳理内层/外层提交，设计执行幂等、租约恢复及 Trip 版本并发保护，并补真实数据库故障/并发测试。
2. P1：修复多地点 action 被去重、状态首次插入竞争和历史清空语义；隔离 E2E 数据并确保失败清理。验收必须包括多个地点同批修改、并发领取、执行中断、超期和重启。
3. P1：建立正式 Web 认证/安全基线（手机号所有权验证或明确选择其他提供方、可过期/可撤销 session、服务端退出、生产 CORS/DEBUG、限流及安全测试）。账号模型预留多提供方绑定；微信小程序是中阶目标，软件端是最终方向，当前优先 Web/API，不提前开发原生壳。
4. P2：方案差量比较、严格时间窗策略、持久化地图预算/缓存与跨进程同步；社媒链接自动读取先调研官方接口/授权方案，不绕过平台限制。DeepSeek 重复前缀真实付费缓存实验尚未执行，需单独确认预算。
5. UI 大改按用户意愿暂缓；后续先补 1280×720、账号切换、多端协作、评价/偏好保存清空的浏览器回归，再做信息架构/组件拆分与 CI。

以下为已完成的历史排期核对，不是新的待开发清单：

0. 2026-09-27 新增事项中，账号切换表单清理、地点级固定到访时间窗首版和可感知 LLM 行程助手均已完成；正式认证和合规社媒链接自动导入尚未完成。
1. 已完成首批：统一 Trip 主方案类型与多人客户端语义，隔离共同约点/多地点结果和地图图层，并为约点 Top 3 增加路线步骤与 geometry。
2. 已完成首版文本地点消歧：使用城市上下文和 POI 多候选，非唯一精确结果必须人工确认。
3. 已完成高德 `10021` 首版保护：首次超限后进程内熔断 15 分钟，并提供需登录的无敏感信息调用统计；后续仍需持久化预算、缓存和控制台配额监控。
4. 已修复个人出发起止时间及 Phase 2B 规划时间重复输入：打开 Trip 自动回填个人设置和 Trip 规划参数，实时刷新不覆盖未保存编辑。
5. 已将地点评价和停留时间从浏览器弹窗改为地点卡片内联表单；2026-09-22 内置浏览器人工验收确认提交、刷新回填、修改、校验和取消均可用，并据此修复布局与状态显示回归。
6. 已完成 DeepSeek usage/cache 首版可观测性：页面显示单次 token/cache 数据，诊断接口提供进程内汇总；真实重复前缀的付费实验尚未执行。
7. 已完成 Phase 4A-3 首版：解析响应带 session/candidate UUID，前端接受、修正和拒绝均写入严格脱敏快照。
8. 已完成 Phase 4B 展示、管理和候选软推荐；少于 3 条有效反馈或关闭个性化时不加分，核心路线排序始终不变。
9. Phase 4 页面首轮人工验收已完成；仍建议后续引入可持续运行的浏览器自动化。Phase 3B 的“用户粘贴正文 + 评论”和手动导出适配首版已完成。


## 11. 下一位 Codex Agent 开始工作前必须阅读的文件

按顺序阅读：

1. `docs/PROJECT_HANDOFF.md`：当前真实状态与验证基线。
2. `docs_private/AGENTS.md`：本地开发命令、编码规则和禁止破坏的行为（当前文件为私有且不会上传 Git）。
3. `docs_private/出行规划Agent-产品与技术方案.md`：产品总目标和阶段边界。
4. `docs_private/Phase2-多人多地点联合路线设计.md`：当前核心路线语义、算法和约束。
5. `docs_private/Phase3-分享文本与地点知识设计.md`：分享链接、规则/LLM 合并与人工确认设计。
6. `docs_private/Phase4-历史反馈与个性化设计.md`、`docs_private/Phase5-Agent对话整合设计.md`：已完成基础范围与后续 Agent 设计。
7. `docs_private/recording.md`：历史修改和验证记录；注意早期顶部进度可能过时，应以靠后的记录和当前代码为准。
8. `app/models.py`、`app/routers/agent.py`、`app/services/agent_assistant.py`、`app/services/itinerary.py`、`migrations/versions/0009_agent_state.py`、`app/security.py`、`app/services/ws.py`：当前 Agent/安全风险的直接核查范围；再读 `frontend/index.html`、`tests/test_phase1.py`、`test_e2e.py` 及对应业务路由。

开始修改前还必须执行 `git status --short`，确认没有覆盖用户未提交工作。

## 12. 重要设计决策以及原因

1. **第一站同时作为集合目的地。** 这使多人从不同起点出发与共同游览多地点成为一个连续问题，避免另设无关集合点增加总成本；原约点功能保留为辅助流程。
2. **允许单人、多地点。** 核心算法不能假设至少两人，这既满足独自出游，也使多人功能成为自然扩展。
3. **个人赴约与共同游览分开计算。** 第一段按每位参与者自己的交通方式，集合后按 Trip 的共同交通方式，更符合真实出行。
4. **真实路线优先、直线估算只降级。** 高德成功时保存道路/公交 geometry 和步骤；失败时明确提示不可导航，避免把估算伪装成真实结果。
5. **规划结果使用版本快照。** 任何影响规划的输入变化都会递增 `input_version`，旧方案不可确认，防止用户确认与当前地点/时间不一致的路线。
6. **`must_visit` 不可被时间优化器跳过。** 用户明确意图优先于自动优化；只有 `optional` 可以在时间不足时舍弃并说明原因。
7. **LLM 负责理解与草案，确定性服务负责事实与执行。** 已由可选文本提取扩展到对话编排；地点写入仍需 schema、地理编码和人工确认，路线事实来自规划服务，最终确认独立执行。草案不能绕过权限和输入版本检查。
8. **高德链接确定性解析优先于 LLM。** 短链/POI 参数包含明确地点事实，成本更低、结果更稳定；LLM 主要补自然语言地点提取。
9. **原文最小化是设计目标，不是当前全链路保证。** Phase 4 反馈只保存必要的结构化候选及动作，不积累原文训练数据；Agent 原始问答仅短期内存保存，但导入草案的 `import_text` 当前落库，须按第 8 节修正，不能宣称所有原文均未持久化。
10. **当前允许同账号多会话。** 在产品形态和正式账号体系未确定前，不贸然加入单设备踢出机制；`BroadcastChannel` 只解决同一浏览器标签的状态一致性。
11. **Phase 4 将“确认”与“完成”拆开。** 路线确认只是锁定计划，实际完成后才适合收集反馈和生成历史偏好；混用 `finished` 会污染行为数据。
12. **私有设计文档与公开交接分离。** `docs_private/` 被 Git 忽略，用于内部记录和评测数据；本文件位于 `docs/`，可随仓库交接，但不得包含 `.env`、Key、密码或 token。

## 常用启动与验收命令

### 本轮 Git 快照与交接注意事项

开始核查时 `git status --short` 如下，本轮只进一步编辑本文件，其他改动均为既有工作，未提交、未回退：

```text
 M .gitignore
 M app/models.py
 M app/routers/agent.py
 M app/services/agent_assistant.py
 M app/services/itinerary.py
 M docs/PROJECT_HANDOFF.md
 M frontend/index.html
 M scripts/verify_trip_deleted.py
 M test_e2e.py
 M tests/test_phase1.py
?? migrations/versions/0009_agent_state.py
```

最近提交为 `86a9fe2`（阶段性完成提交20260927），此前为 `fc804c0`（Phase4阶段性完成待验证20260922）、`acc201c`（Phase阶段性提交）。工作区实现不能与这些已提交基线混为一谈。`docs_private/` 和 `/docs/` 有忽略规则，但本交接文件已经被 Git 跟踪；私有文档不会自动随仓库交付。不得记录或提交 `.env`、实际 Key、密码、token。

### 命令

```powershell
conda activate route_plan
python -m alembic upgrade head
python -m uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
```

另一个终端：

```powershell
conda activate route_plan
python -m unittest discover -s tests -v
python -m compileall -q app tests test_e2e.py scripts
```

HTTP E2E 会写数据库和固定账号偏好。先隔离测试账号/数据库或实现完整恢复后，再运行 `python test_e2e.py http://127.0.0.1:8000`；不要为了交接核查直接对用户数据重复执行。
