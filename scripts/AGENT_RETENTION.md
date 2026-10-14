# Agent 原文清理与恢复验收

## 本地独立清理任务

清理进程不依赖 FastAPI/uvicorn。Windows 任务计划以当前用户的 Limited/Interactive 身份调用同一 Conda 环境的 pythonw.exe，隐藏运行，不保存密码。

- 登录时启动；每分钟触发一次监督，IgnoreNew 防止重复实例。
- 异常退出最多重启 3 次，间隔 1 分钟；后续周期触发也可再次启动。
- 每 5 秒清理到期原文、终态 actions、到期结构化摘要及超过保留期的回执。
- 单次扫描超时 10 秒；失败写入错误类型并重试，不记录 SQL、数据库 URL 或原文。
- 心跳位于 logs/agent_retention_status.json，已被 Git 忽略。30 秒内成功且连续失败为 0 才认为健康。
- 任务名由仓库绝对路径派生。移动项目之前先卸载旧任务，再在新路径安装；代码和解释器路径必须持续可用。

安装（项目根目录，替换解释器路径）：

~~~powershell
.\scripts\manage_agent_retention_task.ps1 -Mode Install -PythonPath 'D:\anaconda3\envs\route_plan\python.exe'
~~~

查看任务配置及健康状态（失败/过期返回非零）：

~~~powershell
.\scripts\manage_agent_retention_task.ps1 -Mode Status
python scripts/cleanup_agent_state.py --status
~~~

手动扫描、独立常驻和卸载：

~~~powershell
python scripts/cleanup_agent_state.py
python scripts/cleanup_agent_state.py --watch
.\scripts\manage_agent_retention_task.ps1 -Mode Uninstall
~~~

当前本机任务：TripPlanner-AgentRetention-0722526845D8。已核验 Running、Hidden、Limited、Interactive、IgnoreNew、无限运行时长、登录及每分钟触发，并读取到健康的连续成功心跳。TaskScheduler 的 LastTaskResult 也可能描述“已有实例时忽略新触发”；判断清理是否成功应使用上述心跳检查。

## 保留期限的边界

草案 10 分钟后不得执行，成功或撤销后立即清空 actions。扫描带来调度延迟，数据库锁/故障也可能延迟删除。当前任务仅在用户登录且电脑运行时可靠执行；注销、关机、休眠或数据库不可用时不保证按时物理清理。WAL、备份和 PostgreSQL 旧行版本不由应用清理任务安全擦除。

该配置补齐了本机“Web 服务关闭但仍需清理”的缺口，不能视为生产级严格十分钟物理删除保证。无人值守部署应选择服务账号/系统服务或数据库侧调度，并把 --status 非零接入监控；目前没有安装外部告警或修改数据库备份策略。

## 可重复验收

~~~powershell
$env:AGENT_DB_TESTS = '1'
python -m unittest discover -s tests -q
~~~

默认不设置 AGENT_DB_TESTS 时跳过 PostgreSQL/子进程测试。测试用随机 agent_test_ schema，退出时清理；不创建或修改现有用户数据。

真实 OS 子进程验收覆盖：

1. 计算中终止后，等待真实数据库租约到期，由新进程恢复。
2. 业务写入已 flush、尚未 commit 时终止，PostgreSQL 回滚；新进程只提交一次。
3. commit 后响应返回前终止，新进程重放回执，不生成重复路线。
4. 无 Web 服务时独立 worker 清除到期原文；临时使测试表不可用后健康变为失败，恢复表后自动恢复健康。
5. 在随机 schema 的独立 HTTP 服务中执行原 test_e2e.py 完整流程；token 只在子进程内存中保存，地图/LLM 为测试替身。

子进程测试将租约缩短为两秒以加速验收，生产值仍为两分钟。完整 E2E 验证接口和规则闭环，不代表真实高德/LLM 可用。请优先运行隔离包装器，不要直接对现有服务运行使用固定手机号的旧脚本。

仅运行完整 HTTP 隔离验收：

~~~powershell
$env:AGENT_DB_TESTS = '1'
python -m unittest tests.test_agent_process.AgentProcessTests.test_full_legacy_e2e_over_real_http_is_isolated -v
~~~

## 认证数据清理（2026-10-12）
迁移0011后，同一清理循环还会清空过期验证码摘要，删除2天前短信配额记录及过期/撤销会话。更新代码后运行管理脚本 -Mode Restart 重载；该入口先验证任务属于本 checkout。详见 PHONE_AUTH.md。

账号密码迁移0012后，清理循环额外删除一天前的登录/注册限流记录。已重载任务。
