# 手机号验证码登录

> 当前页面已切换账号密码，见 PASSWORD_AUTH.md。本文短信API与适配器保留备用，以下旧页面验证码操作属于历史说明。

当前已实现验证码流程、数据库会话和浏览器界面。**已选择阿里云并完成适配器，资质及真实短信联调待完成**；默认 `SMS_PROVIDER=disabled` 返回 503，不会发送短信。不能按生产可用登录交付。

## 阿里云短信接入准备（2026-10-12）

服务商已确定为阿里云，资质暂未申请完成。异步发送适配器已实现；默认关闭，不需要现在提供凭证或申请测试发送。

```dotenv
SMS_PROVIDER=aliyun
ALIYUN_SMS_ENABLED=false
ALIYUN_SMS_ACCESS_KEY_ID=
ALIYUN_SMS_ACCESS_KEY_SECRET=
ALIYUN_SMS_SIGN_NAME=
ALIYUN_SMS_TEMPLATE_CODE=
ALIYUN_SMS_CODE_PARAMETER=code
ALIYUN_SMS_TIMEOUT_SECONDS=8
```

等待资质期间保持 enabled=false，或维持默认 SMS_PROVIDER=disabled；本地功能验收仍用前文 mock。资质、签名、模板审批通过后，填写本地环境变量并显式 enabled=true。启动检查会拒绝启用但配置不完整的情况；发送前再次检查，未启用不占验证码发送配额。凭证使用 SecretStr 避免配置 repr 明文展示，不返回前端。

适配器使用固定中国站 HTTPS 地址和异步 httpx，无新依赖，ACS3-HMAC-SHA256 签名。手机号和验证码位于 POST 表单体，禁止重定向、环境代理和自动重试；整体发送时限默认8秒。仅 HTTP 200 且业务 Code=OK 才视为受理，供应商错误、非 JSON 和超时均变成不含请求/响应原文的失败。超时结果不确定，不自动重发。API受理不等于运营商已送达，真实送达、权限和模板参数仍须后续联调。

本轮新增6项离线适配器测试，使用官方固定签名向量与 MockTransport；全量124/124通过（约27.8秒），没有联系阿里云或发送短信。实际 .env 未修改，无新数据库迁移。代码未提交。

接入依据：[SendSms API](https://api.aliyun.com/document/Dysmsapi/2017-05-25/SendSms)、[阿里云 V3 签名机制](https://help.aliyun.com/zh/sdk/product-overview/v3-request-structure-and-signature)。

## 本地验收

先执行 `python -m alembic upgrade head`，最新迁移为 `0011_phone_auth`。账号、Trip 和偏好保留；旧 `.token_store.json` 不再读取，也不会迁移其中的免验证凭证，用户需重新验证。

在本机 .env 设置：

```dotenv
APP_ENV=development
AUTH_SECRET=<至少32个随机字符，在本机生成，不提交>
SMS_PROVIDER=mock
AUTH_COOKIE_SECURE=false
AUTH_SESSION_DAYS=7
CORS_ORIGINS=[]
```

可用 `python -c "import secrets; print(secrets.token_urlsafe(48))"` 在自己的终端生成 AUTH_SECRET。不要在聊天中提供密钥。本轮没有修改实际 .env。

启动/重启 Web 服务后：输入手机号 → 获取验证码 → 页面明确显示“本地模拟短信”及测试码 → 输入测试码登录。mock 只允许 development 和回环来源 IP；不会发送真实短信。生产配置禁止 mock。可信反向代理要严格设置 Uvicorn 的 forwarded-allow-ips，不能信任任意客户端传入的 X-Forwarded-For。

## 接口和行为

- `POST /auth/sms-code`：`{"phone":"13800000000"}`，返回 sent、retry_after=60、expires_in=300；仅本地 mock 返回 dev_code。
- `POST /auth/login`：phone、6位 code、可选 name。首次验证创建账号；已有手机号复用同一账号。原手机号直接登录与占位微信 code 入口均拒绝。
- 浏览器增加 `browser:true`，获得 HttpOnly、SameSite=Lax Cookie，不返回 token；页面不再持久化 token 或用户摘要到 localStorage。
- API 客户端默认获得随机 token，沿用 X-Token 请求头，数据库仅保存 SHA256 摘要。
- `GET /auth/me` 验证并恢复身份，刷新不会自动打开旧 Trip 或恢复地图选点。
- `POST /auth/logout` 撤销当前会话并删除 Cookie。多设备会话互不踢出；浏览器主动切换账号时撤销旧浏览器会话。
- Cookie 写请求要求 `X-Requested-With: TripPlanner`，并验证 Origin；CORS 默认仅同源，额外来源需精确列出。
- WebSocket 使用 Cookie 或 X-Token 头，不再接受 query token；检查 Origin、会话和成员身份，连接后每次收消息及最长每10秒重新校验。退出后已有连接最多约10秒关闭；已在处理中的请求不保证被中断。

## 验证码和保留

验证码由密码学随机数生成，5分钟有效，只能成功使用一次，最多5次错误；重新发送立即废止旧码。数据库只保存带服务端密钥的 HMAC，不保存明文手机号、IP 或验证码于短信记录。手机号仍作为账号字段保存在 users。

同一手机号60秒冷却、滚动24小时10次；同一来源 IP 滚动1小时30次。PostgreSQL 事务 advisory lock 在多进程之间串行化限流与消费。发送前提交 sending 预留，发送成功才允许校验；失败/超时清空摘要、计入配额，不自动重发收费短信。未知结果须稍后重新获取。

验证码消费、账号创建和会话创建在同一事务。会话默认绝对7天过期，可配置1～30天。过期验证码清空摘要，短信配额元数据保留2天；过期或撤销会话删除。清理复用项目独立 retention 任务，每5秒扫描；升级后应通过 `manage_agent_retention_task.ps1 -Mode Restart` 重载。停机、数据库故障、WAL/备份的边界沿用 AGENT_RETENTION.md；逻辑过期始终由请求校验。

## 生产接入仍需完成

1. 阿里云适配器已实现，等待资质、短信签名和验证码模板审批；保持真实发送关闭，通过审批后填入环境变量并进行真实联调。
2. 生产设置 APP_ENV=production、DEBUG=false、AUTH_COOKIE_SECURE=true、随机 AUTH_SECRET、HTTPS 和精确来源。启动时拒绝不安全配置；跨 worker 必须共享 AUTH_SECRET，轮换会使待验证验证码和限流键失效，需安排过渡。
3. 真实短信到达率、供应商配额、费用监控与告警、运营商限制和人工页面验收。短信账号登录不包含额外多因素认证或手机号换绑/回收处理。
4. CORS 不会代替代理访问控制。部署时验证可信代理、Host/Origin 与 HTTPS 转发配置；同源部署是当前前端的支持方式，跨站 Cookie 部署未验收。

## 验证记录

```powershell
$env:AGENT_DB_TESTS = '1'
python -m unittest discover -s tests -q
```

2026-10-12：118/118 通过（约27.3秒），含19项新增认证测试、隔离完整 HTTP E2E、真实进程中断恢复和迁移升降级。首轮全量115项中有两项既有子进程超时，未修改超时阈值；补充认证测试后复跑118项全部通过，保留该首次失败记录。

compileall、前端 node --check、git diff --check 通过。未发送真实短信；可用浏览器为空，人工页面验收未完成。旧 test_e2e.py 现在要求 mock 返回验证码，仍使用固定手机号，优先运行随机 schema 的 tests.test_agent_process 隔离包装器。

设计参考：[OWASP 会话管理](https://cheatsheetseries.owasp.org/cheatsheets/Session_Management_Cheat_Sheet.html)、[OWASP CSRF 防护](https://cheatsheetseries.owasp.org/cheatsheets/Cross-Site_Request_Forgery_Prevention_Cheat_Sheet.html)。
