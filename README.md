# 多人出行规划 Agent

FastAPI + PostgreSQL + 原生 HTML/JavaScript 的出行规划工具，支持多人不同起点、个人赴约交通方式、多地点联合路线、协作投票、分享文本导入、行程评价、偏好及受控 Agent 草案。

当前默认使用**用户名和密码注册/登录**，不依赖短信资质。手机号验证码与阿里云适配器保留备用，默认不发送短信。当前是可运行的开发版本，生产部署、跨进程实时广播和部分路线约束仍待完善。

## Agent工程与作品集

架构职责、受控执行、演示脚本和真实能力边界见[Agent工程说明](AGENT_ENGINEERING.md)。新增[31条公开合成行为评测](evals/README.md)，默认离线、不执行业务写入，生成任务检查、动作指标、延迟和可复现指纹报告。

    python scripts/evaluate_agent.py

同一离线回归集修复前25/31、修复后31/31；这不是线上准确率或真实推荐质量。模型评测需要显式开启并限制样例数。

## 本地启动

需要 Python 3.11 或更新版本、PostgreSQL，以及用于前端语法检查的 Node.js。已有 Conda 环境可使用：

```powershell
conda activate route_plan
python -m pip install -r requirements.txt
```

首次设置才复制模板；已有 .env 不要覆盖：

```powershell
Copy-Item .env.example .env
```

在本机 .env 填入已创建数据库的 DATABASE_URL。地图和LLM为可选配置，不配置时使用明确标注的估算/规则降级；不能把估算结果用于导航。密码登录开发模式不需要短信凭证。

```powershell
python -m alembic upgrade head
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload
```

打开 http://127.0.0.1:8000 注册新账号。用户名3～32位字母、数字或下划线；新密码8～15字符。API文档：http://127.0.0.1:8000/docs 。刷新会恢复有效登录，不会自动打开旧Trip或恢复地图选点。

已有手机号账号的数据保留，绑定/重置需本机管理员核验用户ID；没有默认密码；已预先保存一次性恢复码的账号可自助重置，其他情况由可信管理员核验。详见[账号密码说明](scripts/PASSWORD_AUTH.md)。真实 .env、密钥、密码及token禁止提交。

## 收藏与Agent推荐

新增个人地点收藏、标签和私人备注，以及同行可见的行程地点评论。侧栏可选择收藏、共同约点、路线规划和行程助手；Agent支持多人餐饮推荐、品牌分店比较及指定区域半日游，先预览再确认加入。使用方法与计算边界见[SAVED_PLACES_AGENT.md](scripts/SAVED_PLACES_AGENT.md)。当前迁移为0015_recovery_code，共同返程终点可保存到正式路线。

## 服务检查

- `GET /health`：进程存活，数据库故障时仍可返回200。
- `GET /ready`：在3秒期限内取得数据库连接并执行只读SELECT 1；成功返回200，连接失败或超时返回503，响应不包含内部错误并禁止缓存。
- 部署流量准入使用 `/ready`；进程存活探针使用 `/health`，避免数据库短暂故障引发反复重启。就绪检查仅验证数据库连通性，不保证迁移版本、地图/LLM或独立清理任务健康；仍需先执行迁移并单独监控清理任务。

## 验证

```powershell
python scripts/check_frontend.py
python -m compileall -q app tests scripts migrations test_e2e.py
$env:AGENT_DB_TESTS = '1'
python -m unittest discover -s tests -q
git diff --check
```

全量测试包含真实PostgreSQL、随机schema迁移、并发/回滚、真实进程中断恢复及完整HTTP E2E；数据库账号需有CREATE SCHEMA权限。可用TEST_DATABASE_URL指向独立测试数据库。测试不会调用真实短信、LLM或地图服务，不会修改原账号或token文件。未设置AGENT_DB_TESTS时数据库测试跳过，不能据此称全量通过。

完整E2E单独运行：

```powershell
$env:AGENT_DB_TESTS = '1'
python -m unittest tests.test_agent_process.AgentProcessTests.test_full_legacy_e2e_over_real_http_is_isolated -v
```

该包装器创建独立schema与临时HTTP服务，并清理自建进程与数据。不要直接对现有服务运行test_e2e.py或手动绕过其隔离标记。

GitHub Actions工作流位于[regression.yml](.github/workflows/regression.yml)：临时PostgreSQL16、Python3.12、Node22，检查迁移、Python/JavaScript语法及全量测试。只需仓库读权限，不引用真实凭证，不部署应用。已接入离线Agent行为评测及Job Summary，尚未推送或执行GitHub云端任务。

## 运行和剩余边界

- Agent草案有执行租约、数据库事务与重放回执，导入原文短期有效并定时清理；独立任务的注销/休眠/故障和备份保留边界见[清理说明](scripts/AGENT_RETENTION.md)。
- 首站即集合目的地；每人分别赴约，之后共同游览。可选地点可被跳过，必去地点不能跳过。旧版本方案不能直接确认。
- 固定时间窗支持等待与有界顺序调整，仍无法满足时明确提示冲突，当前算法不保证全局最优或严格可行；共同返程已支持；提前离队和各自返家尚未实现。
- WebSocket房间及部分缓存/诊断仍为进程内状态，当前不要宣称多worker广播已打通。
- 生产需HTTPS、关闭DEBUG、Secure Cookie、正确可信代理/CORS、独立清理服务、监控和恢复策略。账户入口、昵称/密码及偏好已获用户人工验收；会话管理已补手机页面验收；恢复码的密码重置由隔离API回归覆盖。
- 不自动抓取第三方社媒；地点导入仍需人工确认。

部署准备及验收见[DEPLOYMENT.md](scripts/DEPLOYMENT.md)。

当前实现、验证记录和下一步任务见[项目交接](docs/PROJECT_HANDOFF.md)；短信备用配置见[PHONE_AUTH.md](scripts/PHONE_AUTH.md)。

Agent请求级链路观测与本机排障：[AGENT_OBSERVABILITY.md](AGENT_OBSERVABILITY.md)。

三场景任务评测：[运行说明与能力边界](evals/DISCOVERY_TASKS.md)。设置AGENT_DB_TESTS=1后运行 `python scripts/evaluate_discovery.py`，使用随机隔离schema和离线工具夹具。

## 一键本机演示

运行 `python scripts/run_demo.py`，创建独立演示进程和随机schema，准备4个合成行程、2个账号及5个收藏。终端提供指南URL和短期凭据文件路径；正常停止或1小时TTL到期自动清理。模型为规则降级、道路为估算，不调用真实外部服务。详见[演示说明](scripts/LOCAL_DEMO.md)。

简历与面试准备度、当前可用项目表述及真实模型剩余缺口：[PROJECT_READINESS.md](PROJECT_READINESS.md)。单次模型诊断默认不联网：`python scripts/diagnose_model.py`，仅显式 `--live` 才调用一次真实模型。
