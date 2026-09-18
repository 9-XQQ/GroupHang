# 多人出行规划 Agent：项目交接说明

> 核对日期：2026-09-16（Asia/Shanghai）  
> 核对基线：`main` / `ccc7031`，与 `origin/main` 一致；生成本文档前工作区无未提交修改。  
> 本文以当前代码、迁移、OpenAPI、Git 状态及实际测试结果为准。`docs_private/` 中部分早期进度描述已经过时，不能单独作为当前状态依据。

## 1. 当前已经完成的功能

### Phase 1：账号、Trip 与多人协作

- 使用中国大陆 11 位手机号或开发阶段的微信 code 登录；首次登录自动创建用户，昵称可选，已有用户可更新昵称。
- 创建 Trip、通过邀请码加入、列出本人创建/加入的 Trip、查看详情、创建者删除 Trip。
- 创建者自动成为参与者；成员可分别提交出发点、交通方式、最早出发时间和最晚参与时间。
- 创建者与成员权限校验；非成员不能读取 Trip 数据或建立该 Trip 的 WebSocket。
- 智能约点候选、`minimax`/`min_sum` 排序、赞/踩/取消投票和实时广播。
- 页面刷新后不自动恢复旧账号、旧 Trip 或旧选点；用户主动登录并选择历史 Trip。
- 同一浏览器标签页之间通过 `BroadcastChannel` 同步账号变化；系统仍允许同一账号多端同时登录。

### Phase 2：多人、多起点、多目的地联合路线

- 地点 CRUD、候选/必去/可选/排除状态、地点投票和预计停留时间。
- 单人也可以规划多个地点；多人可以从不同出发点、使用不同交通方式前往第一站集合。
- 创建者设置行程起止时间、共同交通方式以及均衡/总时间/总距离优化目标。
- 在 2～10 个已确认地点内选择第一站，再用最近邻与 2-opt 启发式优化后续顺序。
- 生成每位参与者到第一站的 ETA、共同路线分段、地点到达/离开时间线、总耗时及警告。
- `available_from` 会推迟个人出发；`available_until` 会产生结构化冲突警告。
- 时间不足时允许跳过 `optional`，但不跳过 `must_visit`。
- 规划快照保存参与者路线和共同路线 geometry，保证不同客户端看到同一结果。
- 输入变更会递增 `input_version`；旧方案标记为过期，过期方案不能确认。
- 创建者可确认最终方案，确认后 Trip 锁定；创建者可以重新打开编辑。

### Phase 3：地点搜索和分享文本导入

- 城市手动选择、浏览器/高德定位、城市范围 POI 搜索、候选项确认。
- 规则解析结构化文本、标签文本和竖线分隔文本。
- 解析高德直接链接、短链接和 `wb.amap.com/?p=...` 分享参数。
- 可选 LLM 地点提取，支持 OpenAI-compatible Chat Completions 和路径含 `/anthropic` 的 Anthropic Messages 兼容接口。
- LLM 输出必须通过本地 Pydantic schema；失败时自动回退规则解析。
- 解析结果经过高德地理编码，并要求用户人工确认后才写入 Trip。

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
│     ├─ llm_parser.py        可选 LLM 地点提取适配器
│     └─ ws.py                WebSocket 连接管理与广播
├─ frontend/index.html        当前全部前端页面、样式和脚本
├─ migrations/               Alembic 环境与 0001～0006 迁移
├─ tests/test_phase1.py       33 个 unittest 单元/规则测试
├─ test_e2e.py               需要已启动服务和 PostgreSQL 的完整 HTTP E2E
├─ scripts/evaluate_place_parser.py  规则/LLM 地点解析评测脚本
├─ docs_private/              被 Git 忽略的内部设计、记录和评测数据
├─ docs/PROJECT_HANDOFF.md    本交接文档
├─ requirements.txt
├─ alembic.ini
└─ .env.example
```

路由层负责身份、权限、HTTP 语义与事务；路线算法和第三方调用放在 `services/`。坐标在业务对象中使用 `lat/lng`，调用高德时明确转换为 `lng,lat`。

## 3. 数据库和 Alembic 当前状态

- 数据库：PostgreSQL，异步驱动 `asyncpg`；当前不使用 PostGIS，坐标为 Float/JSONB。
- 实际执行 `python -m alembic current` 与 `heads` 均返回：`0006_trip_primary_workflow (head)`。
- 迁移链：
  - `0001_phase1_baseline`：用户、Trip、参与者、约点结果和投票基础表。
  - `0002_phase2a_destinations`：多目的地、规划参数、`input_version`、`trip_destinations`、`itinerary_plans`。
  - `0003_availability`：参与者 `available_from` / `available_until`。
  - `0004_trip_lifecycle`：`finished → confirmed`，新增 `completed_at` / `completed_by`。
  - `0005_destination_feedback`：已完成行程的逐用户地点评价。
  - `0006_trip_primary_workflow`：创建者控制的 Trip 主方案类型。
- 当前主要表：`users`、`trips`、`trip_participants`、`meeting_point_results`、`trip_votes`、`trip_destinations`、`itinerary_plans`。
- 本次 E2E 成功写入了开发数据库中的测试用户和一个新 Trip（测试输出为 `trip_id=2`）；脚本不会自动清理历史测试数据。
- Phase 4A-1/2 已新增 `completed_at`、`completed_by` 和 `destination_feedback`；解析反馈表尚不存在。

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
| Trip | `GET/POST /trips`、`POST /trips/join-by-code`、`POST /trips/{id}/join`、`GET/DELETE /trips/{id}`、`POST /trips/{id}/reopen` |
| 参与者 | `PUT /trips/{id}/participants/me`、`GET /trips/{id}/participants` |
| 地点 | `GET/POST /trips/{id}/destinations`、`PUT/DELETE /trips/{id}/destinations/{destination_id}`、`PUT .../status` |
| 规划 | `GET/PUT /trips/{id}/planning-settings`、`POST /trips/{id}/itinerary-plans`、`GET .../latest`、`POST .../{plan_id}/confirm` |
| 地点发现 | `GET /api/places/search`、`POST /trips/{id}/shared-text/parse` |
| 约点/投票 | `GET/POST /trips/{id}/meeting-points`、`GET/POST /trips/{id}/votes` |
| 实时同步 | `WS /trips/{id}/ws?token=...` |
| 基础 | `GET /health`、`GET /api/config`、`/docs`、`/openapi.json` |

注意：当前 FastAPI/Starlette 版本会在 `app.routes` 中显示 `_IncludedRouter`，不能只遍历顶层 `APIRoute` 判断业务路由是否注册；应检查 `app.openapi()["paths"]`。

## 5. 分享文本导入地点功能当前实现情况

处理顺序如下：

1. `services/shared_text.py` 抽取 URL，并按确定性规则解析标签和分隔文本。
2. 仅允许高德域名做远程分享链接解析，防止任意 URL 抓取；直接 URL 能解析时不发网络请求。
3. 用户勾选 AI 增强且服务端 LLM 可用时，调用 `llm_parser`；LLM 成功后使用其文本候选，避免规则把整句误识别为额外地点，但高德链接候选始终保留。
4. LLM 不可用、失败或响应不合 schema 时回退规则结果，并返回 warning。
5. 非分享链接候选通过高德 geocode 补坐标；无法可靠定位时标记为未解析，交给用户地图确认。
6. 最多返回 10 个候选；接口本身不直接创建地点，前端必须由用户逐项确认加入。

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

本次（2026-09-16）实际执行：

| 检查 | 结果 |
|---|---|
| `python -m unittest discover -s tests -v` | 39/39 通过 |
| `python -m compileall -q app tests test_e2e.py scripts` | 通过 |
| 前端内嵌 JavaScript `new Function` 语法检查 | 通过 |
| `python -m alembic current` / `heads` | `0006_trip_primary_workflow (head)` |
| OpenAPI 路径生成 | 20 组 HTTP 业务/基础路径正常生成 |
| 启动 Uvicorn 后运行 `python test_e2e.py http://127.0.0.1:8000` | 通过，退出码 0，约 90 秒 |

33 个单元测试覆盖：成员/非成员访问、锁定 Trip 禁止修改、schema 校验、高德路线 polyline、公交步骤、POI 搜索、分享链接安全和坐标解析、单人多地点、可选点跳过、参与者时间约束、路线快照、约点混合交通映射、投票聚合、规则文本解析、OpenAI/Anthropic 两种 LLM 响应。

E2E 覆盖：三用户登录、创建/列出/加入 Trip、非成员 403、两名参与者提交不同出发方式、地点协作与权限、多地点规划与确认/重开、分享文本解析并确认加入、约点、投票/取消以及非法投票 422。真实高德请求成功返回候选和真实路线；部分驾车附加描述仍显示无实时路况估算。

## 8. 当前已知问题

### 需要优先解决

1. Trip 生命周期和已完成行程的地点评价已实现，但解析修正反馈与个性化数据闭环尚未实现。
3. 当前 token 是开发阶段随机 token，持久化于本地 `.token_store.json`；没有过期、撤销、JWT/Redis session、设备管理或正式微信 code2session。
4. 默认 `DEBUG=True`，开发模式的未处理 500 会返回 traceback；CORS 也为全开放。不能直接作为公网生产配置。

### 功能与体验限制

- 路线顺序使用最近邻 + 2-opt 启发式，不保证全局最优；尚未引入 OR-Tools。
- `available_until` 目前主要生成警告；尚未支持参与者中途离队、分支路线和个人返程。
- 营业时间只支持简单的单日 `HH:MM`，复杂星期规则和跨日营业未完整处理。
- 高德不可用时只能降级估算，不能提供可导航路线；外部 API 的配额、网络和权限仍会影响速度与质量。
- 地址 geocode 可能命中同名地点；虽然已有城市 POI 搜索和人工确认，但公园/景区具体入口仍需用户主动选择。
- `frontend/index.html` 是单文件前端，功能已较多，状态与渲染逻辑维护成本正在上升。
- `GET /trips/{id}` 逐个查询参与者用户，存在 N+1 查询；规模扩大前应改为 join/selectin。
- Trip 删除为永久删除，无归档或回收站；开发 E2E 产生的数据不会自动清理。
- 同一账号可多设备/多页面同时在线，这是当前明确允许的开发行为，不应误判为 bug；正式策略尚未确定。

## 9. 尚未完成的任务

- Phase 4A-1 已完成：统一 `active → confirmed → completed` 生命周期，迁移旧 `finished`，增加完成字段/接口、历史筛选和完成后不可变规则。
- Phase 4A-2 已完成：已完成 Trip 的地点访问、评分、实际停留、标签和再访意愿反馈。
- Phase 4A-3：记录地点解析候选被接受/修改/拒绝的脱敏反馈，不保存原始聊天全文。
- Phase 4B：基于本人已完成行程反馈生成可解释偏好，并提供关闭/清空能力。
- Phase 5：对话式 Agent 编排。LLM 目前只做地点提取，不负责权限、写操作或路线事实计算。
- 正式认证、账号安全、设备/session 管理、HTTPS、审计、备份、数据导出/删除和云端部署。
- 更复杂的真实时间约束、参与者提前离队/返程、入口级选点、百度分享链接与坐标系转换。
- 更系统的数据库路由集成测试、前端自动化测试、外部 API mock/契约测试和 CI。

## 10. 下一阶段开发建议

Phase 4A-1/2 后端闭环已完成，但 2026-09-16 手动测试暴露了核心回归。暂缓 Phase 4A-3，按以下顺序处理：

1. 已完成首批：统一 Trip 主方案类型与多人客户端语义，隔离共同约点/多地点结果和地图图层，并为约点 Top 3 增加路线步骤与 geometry。
2. 已完成首版文本地点消歧：使用城市上下文和 POI 多候选，非唯一精确结果必须人工确认。
3. 已完成高德 `10021` 首版保护：首次超限后进程内熔断 15 分钟，并提供需登录的无敏感信息调用统计；后续仍需持久化预算、缓存和控制台配额监控。
4. 已修复个人出发起止时间及 Phase 2B 规划时间重复输入：打开 Trip 自动回填个人设置和 Trip 规划参数，实时刷新不覆盖未保存编辑。
5. 已将地点评价和停留时间从浏览器弹窗改为地点卡片内联表单；仍需补页面视觉验收和浏览器自动化测试。
6. 下一步增加 DeepSeek usage/cache 可观测性；之后再继续 Phase 4A-3。

完整反馈和优先级记录在私有文档 `docs_private/2026-09-16-手动测试反馈与优先级.md`。

## 11. 下一位 Codex Agent 开始工作前必须阅读的文件

按顺序阅读：

1. `docs/PROJECT_HANDOFF.md`：当前真实状态与验证基线。
2. `docs_private/AGENTS.md`：本地开发命令、编码规则和禁止破坏的行为（当前文件为私有且不会上传 Git）。
3. `docs_private/出行规划Agent-产品与技术方案.md`：产品总目标和阶段边界。
4. `docs_private/Phase2-多人多地点联合路线设计.md`：当前核心路线语义、算法和约束。
5. `docs_private/Phase3-分享文本与地点知识设计.md`：分享链接、规则/LLM 合并与人工确认设计。
6. `docs_private/Phase4-历史反馈与个性化设计.md`：下一阶段已确认方案。
7. `docs_private/recording.md`：历史修改和验证记录；注意早期顶部进度可能过时，应以靠后的记录和当前代码为准。
8. `app/models.py`、`app/services/access.py`、`app/routers/trips.py`、`app/routers/itineraries.py`、`frontend/index.html`、`tests/test_phase1.py`、`test_e2e.py`：Phase 4A-1 的直接影响范围。

开始修改前还必须执行 `git status --short`，确认没有覆盖用户未提交工作。

## 12. 重要设计决策以及原因

1. **第一站同时作为集合目的地。** 这使多人从不同起点出发与共同游览多地点成为一个连续问题，避免另设无关集合点增加总成本；原约点功能保留为辅助流程。
2. **允许单人、多地点。** 核心算法不能假设至少两人，这既满足独自出游，也使多人功能成为自然扩展。
3. **个人赴约与共同游览分开计算。** 第一段按每位参与者自己的交通方式，集合后按 Trip 的共同交通方式，更符合真实出行。
4. **真实路线优先、直线估算只降级。** 高德成功时保存道路/公交 geometry 和步骤；失败时明确提示不可导航，避免把估算伪装成真实结果。
5. **规划结果使用版本快照。** 任何影响规划的输入变化都会递增 `input_version`，旧方案不可确认，防止用户确认与当前地点/时间不一致的路线。
6. **`must_visit` 不可被时间优化器跳过。** 用户明确意图优先于自动优化；只有 `optional` 可以在时间不足时舍弃并说明原因。
7. **LLM 只是可选解析增强。** 地点写入仍需 schema 校验、地理编码和人工确认；LLM 失败回退确定性规则，不能直接控制 Trip 或生成路线事实。
8. **高德链接确定性解析优先于 LLM。** 短链/POI 参数包含明确地点事实，成本更低、结果更稳定；LLM 主要补自然语言地点提取。
9. **不默认保存 LLM 原始输入。** 文本可能包含聊天内容和个人信息；Phase 4 反馈只保存必要的结构化候选及用户动作，正式隐私授权前不积累原文训练数据。
10. **当前允许同账号多会话。** 在产品形态和正式账号体系未确定前，不贸然加入单设备踢出机制；`BroadcastChannel` 只解决同一浏览器标签的状态一致性。
11. **Phase 4 将“确认”与“完成”拆开。** 路线确认只是锁定计划，实际完成后才适合收集反馈和生成历史偏好；混用 `finished` 会污染行为数据。
12. **私有设计文档与公开交接分离。** `docs_private/` 被 Git 忽略，用于内部记录和评测数据；本文件位于 `docs/`，可随仓库交接，但不得包含 `.env`、Key、密码或 token。

## 常用启动与验收命令

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
python test_e2e.py http://127.0.0.1:8000
```
