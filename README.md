# Halpha

Halpha 是由单一所有者维护的交易决策、执行与学习工作台。开发采用连续、可验证的小步迭代；当前焦点和环境事实记录在 `docs/L4/HALPHA-PLAN-001-current-plan.yaml`。

依赖、测试、构建和运行命令统一使用由 Python 3.13.14 创建的仓库 `.venv`。测试通过或生成产品构建不允许系统自行发起真实账户交易动作。

## 本地隐私门禁

真实凭据、个人身份、本机路径、代理及启用配置只保存在 Git 外。仓库示例使用合成值；产品不会为遥测、崩溃分析或诊断自动上传本机信息。首次克隆后启用仓库内门禁，并可随时执行全量检查：

```powershell
git config --local core.hooksPath .githooks
python .githooks/check_local_privacy.py --self-test
python .githooks/check_local_privacy.py --all
```

提交门禁检查暂存快照，推送门禁还会检查即将新增到远端的提交历史与提交消息；命中时只报告中性的拒绝类型与待处理位置，不回显或推断内容。Git author/committer 邮箱使用所有者选择的身份，不作为本地隐私门禁项。不得使用 `--no-verify` 绕过。任何外网连接都会在传输层向目标端或代理暴露出口 IP；Halpha 禁止把本机或内网 IP、路径、身份、配置、日志或异常详情再作为请求数据主动附加。

## 统一进程管控

`halpha-control` 是 Halpha 产品长期进程的唯一操作入口。Demo、带单员公域合约实盘和个人合约实盘分别使用 `BINANCE_DEMO`、`BINANCE_LIVE_COPY` 与 `BINANCE_LIVE_PERSONAL` 命名空间下的 App/Executor 任务；三者的数据库、运行身份、凭据、互斥和角色输出互相隔离。`HalphaBackup` 身份和 `\Halpha\Backup` 任务是三个上下文有意共享的辅助设施：它只以各上下文独立的只读数据库角色读取 `halpha_demo`、`halpha_live_copy` 与 `halpha_live_personal`，并分别写入 `backups/postgresql/demo`、`backups/postgresql/live_copy` 与 `backups/postgresql/live_personal`；它没有产品数据库写入、交易所变更请求或 API Key 能力。生命周期入口把这些 Windows 任务与当前仓库及所有已登记 Git 工作树中的产品进程树和 TCP 监听合并为一个视图。手工启动的 App/Executor 或未知项目监听不能归入受管任务时会显示为 `unmanaged:<PID>`，不会被静默忽略。

从仓库根目录执行：

```powershell
# 查看 app、executor、backup 和未登记项目进程/监听
.venv\Scripts\halpha-control.exe status

# 启动产品组合；Demo 启动 app 和 executor
.venv\Scripts\halpha-control.exe start product

# 也可按需单独启动
.venv\Scripts\halpha-control.exe start app
.venv\Scripts\halpha-control.exe start executor
.venv\Scripts\halpha-control.exe start backup

# 停止产品组合；省略服务名时同样默认为 product
.venv\Scripts\halpha-control.exe stop product
.venv\Scripts\halpha-control.exe stop

# 停止指定服务或 status 返回的未登记监听
.venv\Scripts\halpha-control.exe stop app
.venv\Scripts\halpha-control.exe stop unmanaged:12345

# 停止当前环境的受管任务及可证明使用当前配置的未登记 App/Executor
.venv\Scripts\halpha-control.exe stop all
```

仅切换 Windows 任务的开机/看门狗触发资格、但不立即启动或停止当前进程时，使用：

```powershell
.venv\Scripts\halpha-control.exe autostart product on
.venv\Scripts\halpha-control.exe autostart product off
```

`LIVE_READ_ONLY` 的显式观察 Executor 始终保持不自动启动；上述 `product on` 只启用 App。该命令不改变计划、交易权限或交易所状态。

命令默认读取仓库内的 `config/halpha.toml`，日常操作不需要传配置。只有临时检查另一份配置时才追加 `--config <路径>`。CLI 主动生成的帮助、表头、状态、操作结果和错误前缀只使用 ASCII 英文，避免 Windows 终端编码问题；路径、外部错误和日志等被动内容保持来源原文。脚本需要机器读取时追加 `--json`，例如 `.venv\Scripts\halpha-control.exe status --json`。

带单账户与个人账户使用各自的 `LIVE_READ_ONLY` 配置形状完成隔离投影；例如 `--config config/halpha.live-copy-read-only.toml` 只选择带单账户 App。`LIVE_READ_ONLY` Executor 支持两种互斥组合：不带私有凭据的公开前向观察，或带目标账户只读 Key 与 Executor 数据库凭据的私有账户观察。后者只追加账户仓位和开放委托快照，结构上不装载执行客户端、动作 repository、Coordinator 或交易所写能力。具体凭据、账户事实、受管任务状态和资格结果只存在于仓库外的本机运行边界，不在 Git 中记录；私有只读观察不需要写门，也不代表外部仓位已被 Halpha 接管。真实交易权限和 `LIVE_WRITE` 写门仍须另行授权与投影。

进入 `LIVE_WRITE` 后，Executor 还会用写门指纹绑定的同一 Key 调用 Binance 签名 GET 验证账户类型：带单上下文要求当前为带单员且产品 symbol 位于带单白名单，个人上下文要求当前不是带单员。验证失败或超过短期缓存期限时拒绝新增风险；既有责任仍只能按只恢复边界处理。

`status` 显示 `Controlled` 时退出码为 `0`；任务缺失、状态无法确认或发现未登记项目进程/监听时显示 `Attention Required` 并返回退出码 `3`。启动 App 或 Executor 前还会拒绝同类未登记实例，防止出现第二运行者。`stop app`、`stop executor` 和 `stop all` 会先禁用相应 Windows 任务，避免每分钟触发器重新拉起；再次 `start` 会重新启用任务。正常停止超时会拒绝假装成功，只有明确接受操作系统强停风险时才对任务管理的服务追加 `--force`。

研究与公开市场监控已分别迁移到 [Halpha-Research](https://github.com/hhxcv/Halpha-Research) 和 [Halpha-Monitor](https://github.com/hhxcv/Halpha-Monitor)。交易内核不引用它们的路径、模块、虚拟环境、数据或服务名称，`halpha-control` 也不发现、启动或停止它们；各仓按自身 README 管理生命周期。禁止绕过统一入口直接长期运行 Halpha 产品；短时测试、构建和迁移不受此限制。

`stop all` 只控制当前配置对应的产品任务和该配置能够管理的辅助任务，并停止命令行可证明使用当前绝对配置路径的未登记 App/Executor。默认 Demo 配置会同时停止共享 Backup，Live 配置不会；明确属于另一配置的进程会跳过，无法可靠归属环境的进程不会被终止且命令返回 `PARTIAL`。使用前应先查看 `status`。进程停止不等于订单、持仓、保护等业务责任已经闭合；需要恢复、退出或接管时仍须遵守正式运行契约。
