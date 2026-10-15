# Agent 行为评测

这套评测回答“助手是否提出了正确、符合用户意图的草案”，与业务回归、路线算法质量和真实推荐质量分别统计。

## 运行

在项目根目录执行：

    python scripts/evaluate_agent.py

默认使用 agent_cases.json 中的31条公开合成样例，禁止网络，不连接业务数据库，不执行草案。JSON和Markdown报告保存在 docs_private/agent_evals/latest。样例失败返回1，运行配置错误返回2；已有真实密钥不会改变默认离线行为。

只运行否定指令类别：

    python scripts/evaluate_agent.py --category negation

真实模型评测需明确指定模式和样例上限，会消耗模型配额：

    python scripts/evaluate_agent.py --mode live --category negation --max-cases 5

live排除模型契约桩样例。模型不可用而回退规则时记为失败；评测“模型＋意图防护＋规则补充”的完整草案输出，不代表未经防护的原始模型能力，也不执行草案或访问地图。

## 指标与报告

样例覆盖规划、比较、切换、地点停留和状态、清除固定窗、未知及歧义地点、否定和解释请求、无效日期、缺失信息、估算声明及模型协议故障。

- task_pass_rate：所有期望检查均通过的样例占比。
- action_precision / action_recall：按动作类型及约定关键字段匹配，重复动作只匹配一次；没有分母时为null。
- unexpected_actions：本应无动作的样例中出现的动作数。
- exceptions：未处理异常的样例数。
- p50 / p95_latency_ms：最近秩法。offline是本机规则/契约开销，live包含网络延迟，两者不能直接比较速度。
- tokens：仅累计供应商返回的usage，不推算费用；未报告usage不等于没有消耗。

离线模式的成功模型契约样例可能返回 llm_used=true，这只是被测API协议行为，实际网络调用仍为0。报告记录模式、network_disabled及real_model_calls；不保存提示词、回答原文、导入文本、密钥或成员坐标。

每份报告包含数据集、系统提示词、Agent源文件SHA256、Git版本及工作区是否有未提交修改。修改样例后应更新数据集版本并建立新基线。

## 已保存的离线基线

同一数据集SHA256的实际运行结果：

| 指标 | 修复前 | 修复后 |
|---|---:|---:|
| 样例通过 | 25/31 | 31/31 |
| 意外提出的动作 | 3 | 0 |
| 未处理异常 | 2 | 0 |

证据见[修复前JSON](baselines/offline_before.json)和[修复后JSON](baselines/offline_after.json)。失败包括“生成当前路线”漏识别、否定指令仍产生草案，以及无效日历/反向时段导致异常。修复统一模型与规则的意图防护，补充措辞并捕获无效输入。

31条是已知合成回归集，修复针对其中失败案例，不能声称线上准确率100%、覆盖全部提示注入或有统计泛化保证。后续应增加独立保留集、三场景任务集、多轮指代和模型版本对比。权限、幂等、租约恢复和数据库原子性另由隔离业务回归验证。

GitHub工作流已配置离线门禁和Job Summary；是否在云端通过，以实际workflow运行结果为准。

## 2026-10-15 真实服务可用性记录

本轮5条真实模型否定指令样例全部回退规则，live计0/5，响应约20秒，未获得usage；不能据此衡量模型指令能力，也不能将规则正确计为模型成功。失败原报告保留在本机docs_private，诊断结果见最新交接。

后续单例诊断确认ReadTimeout（约20.46秒），未改生产超时。原5条失败结果的[公开副本](baselines/live_negation_unavailable.json)仅去除模型标识，其余检查和指标保持原样；无usage不代表零费用。

## 三场景补充任务集

新增[三场景任务评测](DISCOVERY_TASKS.md)：与31条动作样例分开，12组24轮，使用真实隔离PostgreSQL和固定工具夹具，覆盖连续条件修改、收藏筛选、追问、故障恢复和估算。它是公开合成回归集，不是第三方盲测或自然语言长期记忆评测。

## 2026-10-15 真实模型恢复与契约改进

本轮真实服务恢复HTTP200。同5条公开规划样例在提示词增加Pydantic JSON Schema前2/5通过、之后4/5通过，5条均有有效模型响应，记录8902 token。剩余settings_transit额外提出generate_itinerary，未执行；[完整对照](baselines/schema_prompt_comparison.json)保留失败。它是模型加规则防护/补全的整体测量，小样本顺序对照，不能作为线上准确率或独立盲测。

新增 `python scripts/diagnose_model.py` 默认只记录脱敏配置摘要，不调用外部服务；显式 `--live` 发送一次公开合成请求，捕获白名单网络阶段/HTTP状态，不重试，不记录URL、主机、headers或响应原文。与原评测相同不执行业务写入。

修复live评测在wait_for父任务读取ContextVar导致usage丢失：现在于调用子任务返回usage。修复前真实成功报告也可能usage为空，不能拿其token零合计证明零费用或比较成本。新增评测器源码SHA256帮助追溯报告口径。
