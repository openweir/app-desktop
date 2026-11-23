# 服务商账户

本文面向开发者，说明怎样登录 OpenWeir 服务商、把账户里的订阅变成配置，以及账户页的行为。决策是
[decisions.md](decisions.md) 的 D55–D59；接口和 JSON 字段的对应写在 `src/openweir/models/provider/types.py`，本文写
它们背后的约定。视觉规则见 [DESIGN.md](../DESIGN.md) 的「添加菜单」「服务商的订阅行」「外链按钮」「金额」
「服务商登录对话框」「账户页」。

## 1. 目标与边界

- **服务商**（Provider）：OpenWeir 面板的一个部署，即 api-edge 和它的用户中心（用户中心把 `/api/*` 转给 api-edge），
  以用户中心的 origin（`https://主机[:端口]`）区分。面板可以自己部署，所以服务商可以有很多个。
- **一个应用，账户可选**（D55）：不登录也照常用链接、文件和节点导入。登录后账户里的每份订阅成为一份
  `kind = "provider"` 的配置，与其他配置并列；分流（订阅自带 / 我的方案）、节点池、覆写、自动更新、D27 和备份照常。
  没有模式切换，不做锁定某个服务商的品牌构建，也不预置服务商：用户输入地址，或在服务商的用户中心打开
  `openweir://provider?url=<origin>`。
- **可以同时登录多个服务商**，每个 origin 一个账户。
- **只读，购买在网页上**（D58）：应用显示权益、钱包、套餐和订单，唯一能改的是自动续费；购买、续费、换套餐、充值、
  支付、改密码和两步验证都打开用户中心，回到应用后重新同步。
- **不改后端**：只用 api-edge 已有的接口。v1 只做应用内的原生登录（D56），没有浏览器登录或设备授权。
- **进程边界**：账户请求都在界面进程里、以当前用户身份发出，与订阅下载和 Google Drive 相同；令牌不进服务和系统扩展，
  交给它们的只有拼好的配置（D57）。控制协议、Go 内核和服务都不改。
- **文案**：界面写「服务商」，英文 "provider"；不写"机场""翻墙"（DESIGN.md「定位中立」）。

## 2. 地址与探测

- **地址**来自登录对话框的第一步，或 `openweir://provider?url=<地址>`。`api.origin(address)` 把它变成 origin：去掉首尾
  空白，没写 scheme 时补 `https://`，只留 scheme、主机和端口，丢掉路径、查询和片段。只接受 https；回环主机
  （`localhost`、`127.0.0.1`、`::1`）另外接受 http，供开发和测试。带用户名或密码、没有主机时报
  `provider_invalid_address`。
- **探测**（`AccountsService.probe`）读三个公开接口：
  - `GET /api/v1/app/site`：站点名、说明、默认语言和货币 `{code, symbol, digits}`。读不出 Site 报
    `provider_not_openweir`，这是判断一个地址是不是 OpenWeir 面板的依据；
  - `GET /api/v1/app/auth-settings/providers`：启用的登录方式（`type`、`options`）；
  - `GET /api/v1/app/captcha`：要求人机验证的场景（`scenes`，没有启用人机验证时为空）。
- 探测不登录任何东西。深链只打开对话框并填好地址，由用户继续：与 D45 的导入深链一样，任何网页都能打开它。
- **一个 origin 一个账户**：在已经登录的 origin 上再登录同一个用户（`user_id` 相同）时，换上新令牌，保留记录和配置；
  登录的是另一个用户时（`provider_account_conflict`），问是否替换：标题「替换「a@example.com」？」，正文写会移除它的
  几份订阅。替换就是先退出原账户（4.4 节）再完成登录。

## 3. 登录

### 3.1 方式

v1 只做原生登录（D56）：

| 方式 | 可用的条件 | 请求 |
| --- | --- | --- |
| 邮箱或用户名加密码 | 启用 `email_password`（或 `username`），`scenes` 不含 `sign_in` | 账号含 "@" 时 `POST /api/auth/sign-in/email {email, password}`，否则 `POST /api/auth/sign-in/username {username, password}` |
| 邮箱验证码 | 启用 `email_otp`，`scenes` 不含 `send_code` | `POST /api/auth/email-otp/send-verification-otp {email, type: "sign-in"}`，再 `POST /api/auth/sign-in/email-otp {email, otp}` |
| 两步验证 | 上面的回答是 `{twoFactorRedirect: true, twoFactorMethods}` | `POST /api/auth/two-factor/verify-totp {code}`，带回上一步设下的 cookie |

- 两种方式都可用时，对话框用分段控件「密码 / 邮箱验证码」切换；只有一种时不显示分段控件。手机验证码、邮件链接、
  通行密钥、社交账号和 OIDC 在 v1 都不用。
- **令牌**：Better Auth 的 `bearer()` 插件把会话令牌放在回答的 `set-auth-token` 头里；没有这个头时取回答体的
  `token`。之后的请求带 `Authorization: Bearer <令牌>`。会话是滑动的（7 天不用才过期），没有 refresh token。
  用户 id 取回答的 `user.id`；两步验证的回答没有 `user` 时，用新令牌读 `GET /api/v1/app/me`。
- **两步验证**：第一步的回答设下 Better Auth 的 `two_factor` cookie，有效 10 分钟。这一次登录设下的 cookie 只记在
  `TwoFactorChallenge.cookies` 里：每次登录一份，不和其他请求共用，不落盘。验证时带回去，并带
  `Origin: <origin>`，Better Auth 会检查带 cookie 的请求的来源。v1 只验证身份验证器的代码（`totp`）；超过 10 分钟报
  `provider_two_factor_expired`，回到第一步。
- **登录不了时**：人机验证要在网页里完成，应用做不到。`scenes` 含 `sign_in` 时密码登录不可用，含 `send_code` 时邮箱
  验证码不可用；两种都不可用时报 `provider_captcha_required`。服务商只提供 v1 不支持的方式时报
  `provider_sign_in_unsupported`。两者的文字都说明原因并指向「从链接导入」：在用户中心复制订阅链接，用链接导入。
  人机验证也可能在请求时才出现（400 `MISSING_RESPONSE` 或 403 `VERIFICATION_FAILED`），同样报
  `provider_captcha_required`。
- 服务商按 User-Agent 和 IP 给每次新的登录发一封提醒邮件，这是预期的。

### 3.2 请求的约定

`ProviderApi`（`models/provider/api.py`）的每个请求：

- 在常驻的 daemon 工作线程上发出（`host/workers.py`），跟随系统代理，超时 30 秒；
- `User-Agent: OpenWeir/<版本> (Windows|macOS)`；下载配置（5.4 节）用订阅下载的
  `OpenWeir/<版本> (Windows|macOS) clash.meta`（D48），服务商因此给出 OpenWeir 模板；
- `Accept-Language` 按界面语言写 `zh-CN` 或 `en-US`，套餐名、等级名和说明随之本地化；切换界面语言后重新同步；
- `Accept: application/json`（下载配置和链接时不带，与订阅下载相同）；`/api/auth/*` 的 POST 另带 `Origin: <origin>`；
- **不跟随重定向**，3xx 报 `provider_bad_response`；令牌只发给 origin 本身；
- JSON 回答最多读 1 MiB，配置最多读 `MAX_CONFIG_BYTES`（与订阅下载相同）；
- 错误信息只写状态码和接口返回的错误 `code`，不写 URL、令牌和用户输入的内容。urllib 的 `HTTPError` 带着 URL
  （`url`、`filename`），不能直接用它的文字。错误码见第 10 节。

## 4. 会话与令牌

### 4.1 存放

`ProviderTokens`（`models/credentials.py`）把令牌存在 `KeyringCredentials` 里（服务名 `OpenWeir`，D14）：键
`provider:<服务商 id>`，值是紧凑 JSON `{"token": "…", "user_id": "…"}`。令牌不进数据库、备份、诊断、日志和崩溃记录。

### 4.2 状态

`ProviderState` 只在内存里；启动时凭据库里有令牌就是 `signed_in`。

| 状态 | 含义 | 界面 |
| --- | --- | --- |
| `signed_in` | 有令牌，之后没有请求拒绝过它 | 照常 |
| `sign_in_required` | 没有令牌：请求得到 401（删掉令牌），从备份恢复，或令牌没存下 | 「需要重新登录」，`warning` |
| `account_disabled` | 服务商停用了账户（403），令牌已删 | 「账户已停用」，`danger` |
| `credentials_unavailable` | 读不了凭据库（如钥匙串锁着） | 暂时不知道，下次同步再读，界面上的数据不变 |

后三种都保留配置和它们上次的原文，隧道照常运行；这些配置的自动更新暂停，直到又能用上令牌（重新登录，或凭据库
恢复可读）后的同步。

### 4.3 保活

会话 7 天不用就过期。应用运行时至少每 30 分钟同步一次（5.3 节），同步就是使用，会话一直有效；应用关掉超过 7 天后，
下一次同步得到 401，变为「需要重新登录」。

### 4.4 退出登录

`AccountsService.sign_out`：

1. 尽力 `POST /api/auth/sign-out` 结束会话，失败（如没网）不影响后面的步骤；
2. 删除本机的令牌；
3. 删除这个服务商的全部配置，与订阅页的「删除」相同：正在用的配置由下一份带规则的接替，隧道在运行就重新加载；
4. 软删除服务商的记录，留给备份合并。

确认框是破坏性确认框（`sign_out_removal` 给出内容）：标题「退出「示例服务商」？」，正文写移除几份订阅和之后用
谁的规则，如「将移除 3 份订阅，改用「示例订阅 B」的规则。」。

### 4.5 设备

服务商没有设备注册接口，设备数按在线 IP 计算。账户页显示 `onlineDevices` 与 `deviceLimit`（「2 / 3 台在线」），
「登录设备」打开用户中心的 `/settings/devices`。

## 5. 订阅成为配置

### 5.1 数据

- **表**（数据库版本 4，表都是 STRICT）：
  - `providers`：`id`（uuid）、`origin`、`name`（站点名）、`account`（显示的账户名）、`user_id`、`position`、
    `created_at`、`updated_at`、`deleted_at`；
  - `profiles` 加 `provider_id TEXT` 和 `remote_id INTEGER`；
  - `provider_subscriptions`：`profile_id`（主键）、`data`（`RemoteSubscription.record()` 的 JSON，不含链接）、
    `synced_at`。这是缓存，不进备份。
- `Profile` 加种类 `KIND_PROVIDER = "provider"` 和 `provider_id`、`remote_id` 两项；`url` 为空，订阅链接只在内存里。
- **名称**取 `planName`（null 时用 `planId`），同一账户里重名时加「 · #<id>」。每次同步按界面语言更新，用户不能改名，
  也不能给它链接（`Profiles.edit` 报 `account_profile`）；自动更新的开关和间隔照常能改。
- 服务商的配置不能单独删除，只随退出登录一起移除（`Profiles.remove` 报 `account_profile`）；例外是已不在账户中的
  （`provider_subscription_missing`），它的「更多」菜单有「删除」（`Accounts.removable`）。
- **配置的 id** 由服务商 id 和 `remote_id` 算出（uuid5），共用同一条服务商记录的设备（备份合并过）为同一份新订阅
  做出同一个配置，合并时不会成为两份。删掉后又回到账户中的订阅用回原来那一行，原文和覆写不会回来。
- **已不在账户中**：同步时不在列表里的订阅，删掉它在 `provider_subscriptions` 的行，配置和名称留着。所以服务商的配置
  没有这一行，就是上次同步没找到它（账户需要重新登录时不算，那时由状态说明）。

### 5.2 同步

`AccountsService.sync(服务商 id)`，网络请求都在配置库的锁外：

1. 读令牌：没有时 `sign_in_required`，读不了时 `credentials_unavailable`；
2. `GET /api/v1/app/billing/subscriptions`：每份的 `userId` 必须是登录的用户，id 不重复，最多 128 份，否则
   `provider_bad_response`；
3. 在锁里：新出现的加为配置（排在这个服务商已有的配置之后，没有时排在末尾），已有的更新名称，不在列表里的标为
   「已不在账户中」；账单的用量（`upload`、`download`、`total`、`expireAt`）写进 `Profile.usage`，与订阅下载的
   `Subscription-Userinfo` 同义；存下每份的 `RemoteSubscription`；
4. 在锁外，对可用（`usable`）且还没有原文、或（`planId`、`status`、`usable`、`updatedAt`、`subscriptionAccess`）
   变了的，调用 `profiles.update()`，走 5.4 节取配置。接入方式也比较，是因为它变了取配置的路也变了（比如链接关掉后
   改走应用）。变了的配置同时解除 5.4 节的「保留原文」；变成不可用的不下载，由 5.5 节的状态说明。纯逻辑在
   `models/provider/sync.py`（`checked`、`names`、`reconcile`）。

`complete`（登录完成）做前三步，不下载配置：对话框只等账单，选中的那份由 `use` 下载，其余的由随后的 `sync` 下载。
链接（`url`、`urls`）只留在内存里的同步结果中。

### 5.3 时机

`sync_all()` 不自己定时：订阅页每 60 秒检查一次自动更新（`ProfilesViewModel.checkUpdates` → `profiles.update_due()`），
`update_due()` 先调用 `sync_all()`，再更新到了间隔的配置，与自动更新共用一个计时器。`sync_all()` 按时钟判断每个服务商
该不该同步：

| 时机 | 做什么 |
| --- | --- |
| 启动后的第一次检查 | 本次运行还没同步过的服务商同步（订阅页 `start()` 后 5 秒，`FIRST_CHECK_MS`：先让窗口显示、登录时的连接开始） |
| 运行中，距上次同步 30 分钟 | `update_due()` 里的 `sync_all()` |
| 显示账户页或订阅页 | `sync_all(max_age=60)`：距上次同步超过 60 秒的同步 |
| 登录完成，对话框关闭后 | `sync(id)` |
| 打开用户中心的网页之后 | 打开时调用 `watch(id)`：之后 10 分钟里，距上次同步满 60 秒的 `sync_all()` 都同步它，所以应用重新激活时（`DesktopViewModel.activated` → `ProfilesViewModel.windowActivated`）调用 `sync_all()`，其余由每 60 秒的检查完成 |
| 切换界面语言 | `sync_all(max_age=0)`，名称跟着语言变 |
| 账户页「更多」里的「刷新」 | `sync(id)` |

- **退避**：`sync_all()` 跳过最近失败的服务商：第一次失败后等 5 分钟，连续失败后等 30 分钟；429 带 `Retry-After`
  时至少等那么久。需要重新登录或账户已停用的不同步。`sync(id)` 由用户或登录触发，不等退避，只是在
  `Retry-After` 期间直接报 `provider_rate_limited`，不发请求。
- 配置按自己的自动更新间隔更新（默认取回答的 `Profile-Update-Interval`，服务商写 24 小时），另加同步发现变化时的更新。
- 服务商把渲染好的配置缓存 10 分钟，所以界面不承诺续费或换套餐后"立即"生效。

### 5.4 取配置

`Profiles._refresh` 遇到服务商的配置时调用 `AccountsService.fetch(profile)`。它先读这份订阅的账单
（`GET /api/v1/app/billing/subscriptions/{id}`）：接入方式和状态随时可能变，所以每次都读（D57）。

| 账单 | 做法 | 码（行内文字） |
| --- | --- | --- |
| 401 | 停下，服务商需要重新登录 | `provider_sign_in_required`（需要重新登录） |
| 404，或不在列表里 | 保留原文 | `provider_subscription_missing`（已不在账户中） |
| `status` 为 `disabled` | 保留原文 | `provider_subscription_disabled`（已停用） |
| `expired`，或不可用且已过 `expireAt` | 保留原文，不下载 | `provider_subscription_expired`（已到期） |
| 其他不可用（流量用完、未开始） | 保留原文，不下载 | `provider_subscription_unusable`（流量已用完、未开始） |
| 可用，`subscriptionAccess` 为 `app` 或 `both` | `GET /api/v1/app/proxy/subscriptions/{id}/config`，带令牌和 D48 的 User-Agent；403 `SUBSCRIPTION_APP_DISABLED` 时改走链接 | |
| 可用，`link` | 下载 `url`，连不上时依次试 `urls`；不带令牌、不跟随重定向，链接不落盘 | 没有链接时 `provider_link_unavailable`（没有订阅链接） |

- 回答和订阅下载一样读 `Subscription-Userinfo`、`Profile-Update-Interval` 和 `Content-Disposition`；没有 ETag。
  回答没有 `Subscription-Userinfo` 时，用量留着同步写的账单用量。同步新加的配置先按 12 小时更新，第一次下载后改用
  回答的 `Profile-Update-Interval`（服务商写 24），与导入链接时相同。
- 读到的账单（不含链接）也存进 `provider_subscriptions`，与同步写的相同。
- `KEEPS_CONFIG` 里的码保留上次的原文：不算更新失败（不进 `ProfileList.update_failed`），行内写这个码的文字，不写
  「更新失败」；自动更新不按 30 分钟重试，而是等同步看到这份订阅变了再试。隧道正用着它时照常运行。哪份配置因为
  什么保留原文，在 `AccountSummary.notices`（按 `remote_id`）：订阅一级的码（到期、不可用、停用、不在账户中、没有
  链接）；需要重新登录和账户已停用是整个服务商的，看 `AccountSummary.state`。
- 自动更新（`Profiles._due`）在这些时候等着（`Accounts.paused`）：服务商不是 `signed_in`；这份配置正保留原文；
  存下的账单说它不能下载（到期、停用、不可用），或没有它的账单。
- 正在用的服务商配置不可用、同一账户里另有可用的时，概览提示「改用「X」」，由用户决定，从不自动换（D57）。

### 5.5 状态

`RemoteSubscription.condition(now)` 给出概览、订阅页和账户页共用的状态，账单的 `status` 和 `usable` 优先于本机时钟：

| 条件 | 文字 | 颜色 |
| --- | --- | --- |
| `active` | 有效 | 默认 |
| `expiring`：3 天内到期 | 「3 天后到期」 | `warning` |
| `traffic_low`：已用 90% 以上 | 「流量已用 92%」 | `warning` |
| `expired` | 已到期 | `warning` |
| `traffic_used_up` | 流量已用完 | `warning` |
| `not_started` | 未开始 | `warning` |
| `disabled` | 已停用 | `danger` |
| `unusable`：不可用而账单没说原因 | 暂不可用 | `warning` |

- `trial` 为真时前面加「试用 · 」。
- **下次重置**（`sync.next_reset`）由 `startAt`、`periodStart` 和 `resetMode` 算出：`purchase_day` 与用户中心
  （user-web 的 `nextReset`）和 api-edge 的日任务相同，从购买时起按整月算（1 月 31 日、2 月 28 日、3 月 31 日），取
  `periodStart` 之后的第一个；`month_start` 是 `periodStart` 之后的下一个 1 日零点，api-edge 按服务商的时区算，账单
  不给时区，所以用本机的；`none` 不重置。
- 默认选哪份（登录对话框第 5 步、账户页「当前订阅」）：`sync.most_left`，能下载的里剩余流量最多的，其次到期最晚的。

## 6. 钱包、套餐与购买

### 6.1 只读的信息

| 内容 | 接口 | 显示 |
| --- | --- | --- |
| 当前权益 | 同步存下的订阅 | 套餐、状态、本期流量、重置、到期、设备、自动续费 |
| 钱包 | `GET /api/v1/app/billing/wallet` | 余额、积分、等级和折扣 |
| 套餐 | `GET /api/v1/app/billing/plans`（公开；登录后带上令牌，服务商会加上用户等级可买的） | 名称、流量、设备、各周期的价格、售罄 |
| 订单 | `GET /api/v1/app/billing/orders?page=1&pageSize=5` | 最近 5 笔，待支付的突出显示 |

- 钱包、套餐和订单在每次显示账户页时读取，不存储。
- **金额**是货币最小单位的整数，按站点货币的 `symbol` 和 `digits` 写成「¥15.00」（`models/format.py`）；金额的货币
  代码与站点的不同时（如服务商换过货币之后的旧订单）写代码：「USD 15.00」。金额用等宽数字（DESIGN.md「金额」）。
- **周期**写作月付、季付、半年付、年付、一次性；`traffic_pack` 写「流量包」。
- **自动续费**是唯一能改的（`PATCH /api/v1/app/billing/subscriptions/{id} {autoRenew}`）：开关立即生效，失败时开关
  回到原位，行内用 `danger` 写原因。

### 6.2 用户中心的网页

`models/provider/web.py` 以服务商的 origin 为基址：

| 操作 | 路径 |
| --- | --- |
| 续费 | `/subscription?id=<remote_id>` |
| 更换套餐 | `/plans` |
| 选购某个套餐 | `/plans?checkout=<planId>:<period>` |
| 充值、明细 | `/wallet` |
| 订单、去支付 | `/orders` |
| 密码与两步验证 | `/settings/security` |
| 登录设备 | `/settings/devices` |
| 在用户中心打开 | `/` |

用 `QDesktopServices.openUrl` 在浏览器里打开，之后按 5.3 节同步：在网页上付完款回到应用，权益随之更新。应用里不收款、
不填支付信息，也不显示"购买成功"之类的结论，只显示同步后的结果。

## 7. 界面

视觉规则以 DESIGN.md 为准，本节写行为。

### 7.1 侧栏

「账户」在「设置」之前，只在至少有一个服务商时出现（`ShellViewModel`，机制同 `TUNNEL_PAGES`），Main.qml 多一个
`PageLoader`。停在账户页时退出了最后一个服务商，窗口回到订阅页，焦点交给「添加」。

### 7.2 订阅页

- 标题栏的三个导入按钮合成一个主按钮「添加」，打开菜单：从链接导入…、导入文件…、导入节点…、分隔线、
  登录服务商…。「从链接导入…」打开原来的内联表单，导入深链（D6、D45）照旧直接打开表单。
- **服务商的行**：来源写「示例服务商 · user@example.com」，用量和到期取账单；状态按 5.5 节接在说明末尾。
- **服务商的行的「更多」菜单**：更新、自动更新、编辑覆写…、使用它的规则、加入节点池 / 不加入节点池（与其他订阅的
  行同样的顺序）、分隔线、在网页上续费（已不在账户中的没有）、重新登录…（只在需要重新登录或账户已停用时出现）、
  分隔线、退出「示例服务商」。最后一项是破坏性的，只弹出确认框，所以不以「…」结尾。没有编辑信息…、复制订阅链接和
  删除；已不在账户中的行例外，「删除」在退出之前（5.1 节）。确认框写会删除哪几份订阅（多于 3 份时写前 3 份和总数）、
  之后用谁的规则，以及服务商那边的订阅不受影响。

### 7.3 登录对话框

`ProviderSignInDialog.qml`，普通宽度（`dialog`），选择订阅的一步用宽的（`dialog-wide`）：

1. **地址**：标题「登录服务商」，「服务商地址」输入框，默认按钮「继续」探测。
2. **登录**：标题「登录「示例服务商」」（站点名），下面一行主机名。两种方式都可用时上面是分段控件「密码 / 邮箱验证码」。
   - 密码：「邮箱」（启用用户名时写「邮箱或用户名」）和「密码」，默认按钮「登录」；
   - 邮箱验证码：「邮箱」和「发送验证码」；发出后出现「验证码」（`otpLength` 位）和默认按钮「登录」，60 秒内
     「重新发送」禁用并写剩余秒数；
   - 登录不了时（3.1 节）不显示输入框，正文写原因，按钮是「从链接导入」（关掉对话框，打开订阅页的链接导入表单）和
     「取消」。
3. **两步验证**：标题「两步验证」，「验证码」（6 位），默认按钮「验证」。超时回到第 2 步并说明。
4. v1 没有在浏览器中登录这一步。
5. **选择订阅**：标题「选择要使用的订阅」，账户的订阅列表（单选）：名称、状态和用量。默认选中正在用的那份（重新登录时），
   否则选剩余流量最多的可用订阅；不可用的不能选，第二行写原因，并有紧凑的外链按钮「续费」。按钮是「使用」（默认，
   `AccountsService.use`：下载它的配置，设为当前配置，隧道在运行就重新加载）和「稍后」（只关闭对话框）。
6. **没有订阅**：正文「这个账户还没有订阅」，按钮是外链按钮「选购套餐」（`/plans`）和「完成」（默认）。
7. **只有一份可用、且还没有当前配置**：直接使用它，跳过第 5 步。

- 第 2、3 步另有「上一步」。账户冲突（第 2 节）在第 5 步之前问。
- 输入错误和请求失败用 `input-error` 写在相关输入框下面，对话框不关。进行中控件禁用，默认按钮写「正在登录…」等；
  Esc 等于取消，进行中的请求的结果丢弃。
- `openweir://provider?url=…` 显示窗口、打开对话框，地址已填好，停在第 1 步，焦点在「继续」上；不自动登录。
  对话框已经开着时只更新地址（第 1 步）或忽略（之后的步骤）。

### 7.4 账户页

`AccountPage.qml`，分组排成两列：

- **标题栏**：标题「账户」，状态文字「user@example.com · 示例服务商」，需要重新登录时用 `warning` 写「需要重新登录 ·
  示例服务商」；多个服务商时有下拉框选服务商；「更多」菜单：刷新、在用户中心打开、分隔线、退出登录。
- **左列**：
  - 「当前订阅」：套餐和状态；本期流量（用量条）；重置；到期；设备「2 / 3 台在线」；自动续费（开关）；
    外链按钮「续费」「更换套餐」。当前配置不是这个账户的订阅时，显示剩余流量最多的可用订阅。
  - 「此账户的订阅（3）」：每份一行，名称、状态、到期；正在用的写「使用中」，其余有紧凑按钮「使用」；到期或流量用完的换成外链按钮「续费」，停用和未开始的没有按钮。
- **右列**：
  - 「钱包」：余额、积分、等级；外链按钮「充值」「明细」。
  - 「套餐」：在售的套餐，每个一行，价格按周期；外链按钮「选购」。
  - 「订单」：最近 5 笔；待支付的状态用 `warning`，有外链按钮「去支付」，过了 `expiresAt` 的写「已超时」。金额是
    `price - discount - credit`（与用户中心相同），待支付的写 `amount`。
  - 「账户与安全」：外链按钮「密码与两步验证」「登录设备」；紧凑按钮「退出登录」打开破坏性确认框（4.4 节）。
- **状态**：
  - 需要重新登录：显示上次同步的数据，「当前订阅」第一行是提示和紧凑按钮「重新登录…」；钱包、套餐、订单不读取，
    分组里写「重新登录后显示」。
  - 离线：照常显示上次的数据，状态文字写「上次同步 5 分钟前」。
  - 钱包、套餐或订单读不出来时，在各自的分组里写原因，其他分组照常。

### 7.5 概览

- 「当前使用」用的是服务商的配置时，第二行按 5.5 节用 `notice-warning` 写「3 天后到期 · 续费」「流量已用 92%」
  「已到期 · 续费」；「续费」是链接，切到账户页。
- 正在用的不可用、同一账户另有可用的时，写「改用「X」」（链接，执行 `use`）。
- 没有任何配置的空状态多一个「登录服务商」。

## 8. 存储、备份与诊断

- **备份快照 v2**（D59，`models/backup.py`，格式见 [storage.md](storage.md) 第 3 节）：加入 `providers`，字段为
  `id`、`origin`、`name`、`account`、`user_id`、`position`、`created_at`、`updated_at`，退出登录的记为墓碑；配置
  可以是 `kind: "provider"`，带 `provider_id` 和 `remote_id`，没有 `url`、`path` 和原文。不含令牌、链接和
  `provider_subscriptions`。带 `user_id` 是因为恢复后重新登录要靠它分辨是不是同一个账户（下一条）；它是服务商那边的
  用户 id，不比 `account` 多透露什么。v1 的快照照样能恢复，而且不动本机的服务商和它们的配置（"替换"也一样）；1.x
  读 v2 报 `newer_backup`。
- **恢复**：恢复出来的服务商是 `sign_in_required`，它的配置留着 `provider_id` 和 `remote_id`，没有原文（D27 已处理
  没有原文的配置），账单缓存也没有，要等登录后的同步。重新登录时 `by_origin` 找到恢复出来的记录：同一个 `user_id`
  就把令牌存在它的 id 下，同步按 `remote_id` 对上配置；登录的是另一个账户时按第 2 节先问是否替换，替换会移除恢复
  出来的配置。
- **一个 origin 一个服务商**：两台设备各自登录同一个服务商后互相合并，本机登录着的留下，快照里的那个连同它的配置和
  覆写不写进来，不留墓碑，所以两台设备都不用重新登录，也不会互相删掉对方的账户；正在用的配置换成本机同一账户、
  同一 `remote_id` 的那个。同一份订阅在两台设备上各有一个配置时同理。另一台设备退出登录后，合并它的快照会移除
  这里的这个服务商和它的全部配置（退出登录的墓碑更新时）。细节见 [storage.md](storage.md) 第 3 节"恢复"。
- 恢复之后，`AccountsService` 要从数据库重新读服务商，并删掉已不在的服务商留在凭据库里的令牌（"替换"或合并来的
  退出登录会让本机的服务商记录变成墓碑）。
- **诊断**（`diagnostics.read_accounts`，在工作线程上读）只写白名单里的内容，在 `settings.json` 里：
  - `accounts`：服务商的个数（`providers`）和每个的状态（`states`）：`signed_in`（凭据库里有令牌）、
    `sign_in_required`（没有）、`credentials_unavailable`（读凭据库出错）。令牌只用来判断有没有，不写出去；读数据库
    出错时只写 `{"error": "storage_failed"}`。没读（导出时没传进来）是 `null`；
  - 每份 `kind` 为 `provider` 的配置多一项 `billing`：上次同步的接入方式（`access`）、状态（`status`）和是否可用
    （`usable`），还没同步过是 `null`。接入方式和状态是服务商给的文字，只写应用认识的值（`link`、`both`、`app`；
    `active`、`disabled`、`expired`），其余写 `other`。

  不写 origin、站点名、邮箱或账户名、用户 id、令牌、套餐名和任何链接，凭据库和数据库报错的原话也不写。

## 9. 隐私

[PRIVACY.md](../PRIVACY.md) 要写明：

- OpenWeir 本身没有账户，也没有开发者的服务器；登录服务商是可选的，登录后数据发给那个服务商。
- 离开本机的：登录时的账号、密码或验证码；每个请求的 IP、User-Agent、Accept-Language 和令牌；订阅列表和配置；查看
  账户页时的钱包、套餐和订单请求；运行中至少每 30 分钟一次同步；退出登录时结束会话。
- 留在本机的：服务商地址、账户名和订阅的账单信息在 `openweir.db`；令牌在系统凭据库；钱包、套餐和订单不存；备份含
  服务商地址和账户名，不含令牌。

## 10. 错误码

`ProviderError` 是 `ProfileError` 的子类，码和含义在 `types.py`，文字在 `viewmodels/feedback.py`。

| 码 | 什么时候 | 文字 |
| --- | --- | --- |
| `provider_invalid_address` | 地址不是 https 的 origin | 请输入服务商的 https 地址 |
| `provider_not_openweir` | `/api/v1/app/site` 读不出 Site | 这个地址不是 OpenWeir 服务商 |
| `provider_unreachable` | 连不上、超时、5xx | 无法连接服务商（后接原因） |
| `provider_rate_limited` | 429 `RATE_LIMITED` | 请求过于频繁，请稍后再试 |
| `provider_bad_response` | 重定向；超过 1 MiB 或不是 JSON；缺字段或类型不对；别的用户的订阅；id 重复；超过 128 份 | 服务商的回答无法识别（后接原因） |
| `provider_sign_in_failed` | 401 `INVALID_EMAIL_OR_PASSWORD`、`INVALID_USERNAME_OR_PASSWORD`，或账号格式被拒的 400 | 账号或密码错误 |
| `provider_email_unverified` | 403 `EMAIL_NOT_VERIFIED` | 请先在用户中心验证邮箱 |
| `provider_code_invalid` | `INVALID_OTP`、`OTP_EXPIRED`、`TOO_MANY_ATTEMPTS` | 验证码错误或已过期 |
| `provider_two_factor_invalid` | 401 `INVALID_CODE`、`TOO_MANY_ATTEMPTS_REQUEST_NEW_CODE` | 两步验证码错误 |
| `provider_two_factor_expired` | 超过 10 分钟，或 401 `INVALID_TWO_FACTOR_COOKIE` | 两步验证已超时，请重新登录 |
| `provider_captcha_required` | 要求人机验证（3.1 节），或 400 `MISSING_RESPONSE`、403 `VERIFICATION_FAILED` | 服务商要求在网页上完成人机验证，请在用户中心复制订阅链接，用「从链接导入」添加 |
| `provider_sign_in_unsupported` | 只有 v1 不支持的登录方式 | 这个服务商只能在网页上登录，请在用户中心复制订阅链接，用「从链接导入」添加 |
| `provider_account_disabled` | 登录或登录后的账单请求得到 403 `FORBIDDEN` | 账户已停用 |
| `provider_account_conflict` | 这个服务商已登录另一个账户 | 已登录另一个账户（对话框改为询问替换） |
| `provider_sign_in_required` | 登录后的请求得到 401；没有令牌 | 需要重新登录 |
| `provider_subscription_expired` | 见 5.4 节 | 已到期 |
| `provider_subscription_unusable` | 见 5.4 节 | 按 5.5 节：流量已用完、未开始或暂不可用 |
| `provider_subscription_disabled` | 见 5.4 节 | 已停用 |
| `provider_subscription_missing` | 见 5.4 节 | 已不在账户中 |
| `provider_link_unavailable` | 只能用链接而没有链接；链接回答 403 `SUBSCRIPTION_LINK_DISABLED` 或 404 | 服务商没有提供订阅链接 |
| `provider_app_disabled` | `/config` 回答 403 `SUBSCRIPTION_APP_DISABLED` | 不显示：`fetch` 改走链接 |
| `provider_unknown` | 服务商已经退出（并发） | 服务商不存在 |

`provider_update_failed` 不在其中：它是内核更新代理集合失败的码，与账户无关。

## 11. 代码

| 文件 | 内容 |
| --- | --- |
| `models/provider/types.py` | 值、错误码、`ProviderState`、`ProviderApi` 和 `AccountsService` 两个接口 |
| `models/provider/api.py` | `ProviderApi` 的 HTTP 实现、`origin(address)` |
| `models/provider/web.py` | 用户中心的网页链接（6.2 节） |
| `models/provider/store.py`、`models/database.py`、`models/store.py`、`models/profiles.py` | 表、`KIND_PROVIDER`、配置的两项新字段 |
| `models/credentials.py` | `ProviderTokens` |
| `models/provider/sync.py`、`models/library/accounts.py` | `AccountsService` 的实现，即 `ProfileLibrary.accounts` |
| `models/backup.py`、`diagnostics.py` | 快照 v2、诊断 |
| `viewmodels/provider_sign_in.py`、`ui/components/ProviderSignInDialog.qml`、`desktop/links.py` | 登录对话框、`openweir://provider` |
| `viewmodels/account.py`、`ui/pages/AccountPage.qml`、`viewmodels/shell.py` | 账户页和侧栏 |
| `viewmodels/profiles.py`、`ui/pages/ProfilesPage.qml`、`viewmodels/overview.py`、`viewmodels/feedback.py` | 订阅页、概览、错误文字 |

- `AccountsService` 的方法都是协程，在 asyncio 线程上调用，与 `ProfileLibrary` 相同；改动配置时经过配置库和它的锁，
  网络请求在锁外。`ProfileLibrary.accounts` 是它的实现 `Accounts`，另有给 `Profiles` 用的 `paused`、`removable`、
  `notice` 和恢复备份后的 `reload`。
- 恢复备份（`Profiles.restore`）写完数据后调用 `accounts.reload()`：重新读服务商和账单；`user_id` 没变的服务商留着
  内存里的令牌，从别处恢复来的没有令牌，需要重新登录。恢复后不再有的服务商、或换了账户的（替换，或合并进别的设备
  上的退出登录），删掉它们在凭据库里的令牌。之后服务商的配置也经 `fetch` 重新读取；没有令牌时不发请求，保留原文，
  不算更新失败。重新登录同一个 `user_id` 时用恢复出来的记录，按 `remote_id` 对上配置。
- `RemoteSubscription.record()` 和 `from_record()` 是 `provider_subscriptions.data` 的格式，其中从来没有链接。

## 12. 测试与验收

- **假的服务商** `tests/support/provider_server.py`：127.0.0.1 上的 `ThreadingHTTPServer`，与 `tests/support/google.py`
  相同。提供 site、auth-settings/providers、captcha；邮箱和用户名登录、两步验证的 cookie 和 verify-totp、邮箱验证码、
  sign-out；me；账单的订阅列表、详情、PATCH；钱包；套餐；订单；`/proxy/subscriptions/{id}/config`；令牌链接。可以
  切换接入方式、状态和 `usable`、401、各种 403、429、5xx 和重定向；记下每个请求，用来断言 User-Agent、
  Accept-Language，以及令牌只发给 origin、不跟着重定向、不随链接下载发出。
- **单元测试**：HTTP 客户端、同步、配置库、数据库从 3 升到 4、存储、凭据、备份（v2 往返、v1 可读、不含机密）、深链、
  诊断的白名单；QML 在 offscreen 下测账户页、登录对话框的各步和选择订阅、侧栏、读屏、截图。
- **真实服务商**（[maintenance.md](maintenance.md)）：对一个本地 api-edge 核对两步验证的 Origin 检查、邮箱验证码、
  打开人机验证后的提示、三种接入方式、401 之后的界面和退出登录。

## 13. 风险

- **到期时隧道仍开着**：续费的网页可能要经过已经失效的节点。概览提示续费；用户中心打不开时，用户可以先断开连接。
- **Better Auth 的来源检查**：带 cookie 的两步验证请求要带 `Origin`，以真实的本地 api-edge 核对。
- **要求人机验证的服务商**不能在应用里登录，只能用链接导入。
- **渲染缓存**：服务商把配置缓存 10 分钟，续费后的配置可能晚一点才变。
- **钥匙串提示**：macOS 上应用的签名变了时，读令牌会让系统询问（D14）。
