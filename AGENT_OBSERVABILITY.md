# Agent 请求级链路观测

用于本机排障和工程演示。追踪与草案、执行租约、业务事务及持久回执分离，没有新增数据库表。追踪保存失败不会改变业务返回或替换原异常。

## 查看一次请求

1. 正常登录并进入自己参与的行程，在浏览器开发者工具 Network 中找到聊天或确认执行请求。
2. 成功响应包含服务器生成的 `trace_id`。每次确认重放有独立trace，通过相同的 `proposal_id` 关联。持久业务回执不保存trace_id。
3. 在同一登录浏览器打开 `/trips/{trip_id}/assistant/traces/{trace_id}`；最近追踪列表为 `/trips/{trip_id}/assistant/traces`。
4. 对于进入处理函数后的HTTP业务错误，响应头 `X-Agent-Trace-Id` 提供关联。认证和参数校验在处理函数之前失败时没有追踪；通用500或客户端取消也不保证获得ID，可在列表中查找。

读取同时要求当前行程成员资格与请求所有者匹配；其他同行成员读取该ID返回404。

## 记录字段

| 字段 | 含义 |
|---|---|
| trace_id / proposal_id | 请求关联 / 草案与回执关联 |
| operation / status | chat、execute；ok、error、cancelled |
| started_at / duration_ms | UTC开始时间 / 完整处理函数耗时 |
| spans | 嵌套阶段ID、parent_id、固定阶段名、耗时及状态 |
| error_type / http_status | 异常类名 / HTTP业务错误码，不含异常文本 |
| usage | 仅模型实际返回的非负整数token字段 |
| llm_used / replayed | 是否使用模型结果 / 是否返回已有执行回执 |
| dropped_spans | 超出单请求容量而省略的阶段数 |

阶段包括context.load、assistant.decide、model.complete_json、model.parse_places、discovery.recommend、map.request、route.segment、execution.prepare、execution.commit。缓存命中可能没有外部地图span；并行工具由ContextVar保留各自父节点，列表按开始顺序记录。地图span覆盖一次适配器调用（含内部重试），不逐次记录HTTP尝试。

model阶段的unavailable与assistant阶段的fallback分别表示模型调用未取得可用结果和助手已采用规则；ReadTimeout等白名单错误分类便于区分超时。route.segment的estimated表示实际采用距离/速度估算。成功的execution.commit表示业务提交函数完成；之后广播失败仍可能导致整个请求报错，需结合提交span及重放回执判断。重放不会重新执行工具或提交，span通常为空。

不记录请求参数、原始对话、提示词、模型回复、action内容、坐标、地点地址、私人备注、密码、令牌、密钥或供应商URL。usage未知不代表零费用；span成功不代表模型生成内容正确。

## 容量、期限与限制

完成后最多保留24小时，每进程1000条、每用户跨行程50条，每请求128个span；超额淘汰旧追踪或计数省略。读取和写入检查TTL，应用生命周期清理任务每5秒清理过期追踪。返回深拷贝，调用方修改不会污染记录。

没有持久化、跨worker聚合、外部采集、调用图UI或分布式追踪传播；重启后记录消失，多worker可能读不到另一个worker的记录。仅聊天与草案执行创建根trace，其它接口单独调用不会生成追踪。当前实现用于本机工程验证，部署前再评估OpenTelemetry、受控导出、集中存储及授权删除。

## 可复现验证

```powershell
& 'D:\anaconda3\envs\route_plan\python.exe' -m unittest tests.test_agent_trace -v
$env:AGENT_DB_TESTS='1'
& 'D:\anaconda3\envs\route_plan\python.exe' -m unittest tests.test_agent_execution -q
```

单元测试使用超时桩与合成输入，不访问真实模型或地图；隔离PostgreSQL与HTTP回归验证真实执行、重放、回执未混入追踪和同行成员不能读取。覆盖并发隔离、嵌套父节点、隐私、token白名单、容量、过期、取消、路线估算、观测保存异常与原异常保持。
