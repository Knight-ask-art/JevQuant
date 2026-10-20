# 安全与认证操作说明

> 本文件适用于 JevQuant 的网页登录、模型密钥加密、实盘解锁和订单恢复。
> **不得仅依据静态代码或 Paper 测试认定实盘安全。**

## 1. 网页登录可开关

- **本地、开发环境、合成行情、Paper/Shadow 模式且未启用实盘**：可以在「审计与设置 / Audit & Settings」中开启或关闭网页登录。
- **测试网、实盘或生产环境**：服务器强制要求认证，不能经网页关闭。即使配置文件错误地写了 `JEV_AUTH_DISABLED=true`，非开发 Paper/Shadow 场景的 API 守卫也不会放行。
- 开启登录前须在服务端配置高强度 `JEV_ADMIN_API_KEY`。开启时需要输入该密钥做验证；关闭登录时必须已登录并提交 CSRF Token。
- 浏览器登录后的管理员会话存在 **HttpOnly、SameSite=Strict Cookie**，有效期 8 小时。所有会修改系统的 Cookie 认证请求带 `X-CSRF-Token`。
- CLI 继续可以发送 `X-API-Key`；WebSocket 不接受 URL 查询参数中的 API Key。
- 用户在控制台点击「退出登录」会清理 Cookie；修改服务端管理员密钥会使旧 Cookie 签名失效。
- 登录开关选择写入数据库，正常重启后保留；不安全环境即使保留了「关闭」选项也会强制开启认证。

## 2. 模型密钥加密

初次部署前，在服务端生成 Fernet 密钥（**不要提交到 Git**）：

```bash
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

放进仅服务端可读的 `.env`：

```dotenv
JEV_CREDENTIAL_ENCRYPTION_KEY=将生成的值填在这里
JEV_ADMIN_API_KEY=使用另一个独立的长随机密钥
```

- 浏览器输入的 LLM/Jev API Key 在数据库 `runtime_credentials` 下仅存储 `fernet:v1:<ciphertext>`，接口只返回「是否已配置」。
- 首次启动时对以前留下的 **明文密钥** 进行一次升级：配置了 Fernet 密钥则原地加密；未配置密钥则清除旧值，避免继续明文使用。
- 如果数据库有加密值，而服务端缺失对应 Fernet 密钥或密钥错误，启动会失败关闭，避免把 ciphertext 当作 API Key。
- 不配置加密密钥时，模型密钥仍可通过服务端环境变量注入，但**禁止通过网页保存新密钥**。
- **密钥备份很重要**：丢失后只能重新录入服务商密钥。轮换 Fernet 密钥必须有专用重加密流程，本 PR 不提供无损轮换；不要直接覆盖旧密钥。

## 3. 实盘及恢复边界

- Live 必须经过现有服务器预检、一次性授权 Token、确认短语及 TOTP。认证开关不能绕过 Live 解锁。
- Kill Switch 阻止 **新开仓**，但不阻止已有仓位的确定性止损、止盈及运营人员主动平仓。
- 退出路径应按 **持仓所属模式** 选择订单执行器；服务重启后的 Live 持仓不能误路由到 Paper。
- 对于成交状态不明的订单，采用 `RECONCILIATION_REQUIRED`，不能把 HTTP 超时当成订单失败而重发。
- Hummingbot 实盘成交账本尚无完整真实环境验证。本 PR 仅修复 Kill Switch 阻塞退出的代码分支，不宣称修复全部恢复和对账问题。正式实盘上线前须使用 Testnet 完成断线、部分成交、重启、重复单和主动止损的端到端演练。

## 4. 回归测试

```bash
python -m pip install -e '.[dev]'
python -m pytest -q
cd frontend && npm ci && npm run build
```

GitHub Actions 对 PR 执行 Python 测试及 TypeScript 构建。未经 CI 通过及真实测试网演练，请勿合入后直接开启实盘。
