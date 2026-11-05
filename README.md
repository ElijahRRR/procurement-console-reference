# 采购控制台

自用的订单自动采购系统。订单来自飞书多维表格或手动上传的 Excel／CSV，服务端按规则分配给亚马逊买家账号；对应浏览器里的自有插件领取任务执行，服务端负责护栏、提交登记、结果处理与执行权互斥。

|部分|目录|技术|
|---|---|---|
|服务端|[`backend/`](backend)|Python 3.12+、FastAPI、PostgreSQL 16 / 17、psycopg 3（裸 SQL，无 ORM）|
|控制台|[`web/`](web)（[说明](web/README.md)）|Vue 3、TypeScript、Vite、Element Plus、Tailwind 3，EasyQuick 外观|
|浏览器插件|[`extension/`](extension)（[说明](extension/README.md)）|Chrome MV3、TypeScript、Vite；本机 Chrome 与 AdsPower 都能加载|

本仓库最早是一份参考资料包（第三方采购客户端的调研报告、表格模板、参考插件），这些资料保留在[参考材料](#参考材料)里。

## 当前阶段：只做模拟执行

这是第一个 PR 的交付（[方案 v2](docs/development/采购控制台方案-v2.md) §4）：

- 服务端与控制台：飞书与文件两种订单来源、人审准入、跨来源去重与冲突、建议采购日期门槛、账号自动分配与人工改派、额度、异常处理、结果待核实、事件记录、飞书写回队列与验收。
- 插件：配对、心跳、亚马逊登录身份识别、页面内面板、手动与定时触发的**模拟执行**。

**不会产生真实订单。** 插件不打开、不点击、不修改任何亚马逊页面，结算信息按任务数据推出来；服务端的护栏、提交登记、结果处理、额度和状态机走真实代码路径。服务端本阶段拒绝一切非模拟的执行批次（`REAL_PURCHASE_DISABLED`），模拟结果永不写回飞书，可以在控制台一键清除。

还没有做的：

- **真实下单**：亚马逊页面流程（选择器、页面样本、下单后的等待与保护）在后续 PR 迁入，服务端另加开关。
- **只读查单与物流同步**：第二个 PR。
- **多用户的飞书授权**：目前只有部署者在服务端配置的一套飞书自建应用凭证；每位用户用自己的飞书账号授权（OAuth）以后再评估（[落地方案 v1](docs/development/采购与订单同步系统落地方案-v1.md) §6.1「多人扩展」）。
- 只支持美国站；插件只连本机的服务端（`127.0.0.1` 或 `localhost`）。

## 在 Mac 上跑起来

以下按 Apple 芯片的 Mac、Homebrew 装在 `/opt/homebrew` 写。命令都从仓库根目录开始。

### 1. 安装工具

```bash
brew install postgresql@17 python@3.13 node@22
brew services start postgresql@17

# node@22 不会自动放进 PATH，需要手动加一行（zsh）：
echo 'export PATH="/opt/homebrew/opt/node@22/bin:$PATH"' >> ~/.zprofile
source ~/.zprofile
node --version          # 应显示 v22.x

# 可选：要在终端里用 psql 时，同样把 PostgreSQL 的命令加进 PATH
echo 'export PATH="/opt/homebrew/opt/postgresql@17/bin:$PATH"' >> ~/.zprofile
```

Homebrew 的 PostgreSQL 以当前 macOS 用户为超级用户、本机连接免密码，服务端的缺省连接串 `dbname=procurement` 不用改。

### 2. 安装依赖并构建控制台

```bash
python3.13 -m venv .venv                    # 用 Homebrew 的 Python 3.13；系统自带的 /usr/bin/python3 版本太旧（服务端要求 3.12 以上）
.venv/bin/pip install -r backend/requirements.txt

cd web && npm ci && npm run build && cd ..  # 产物在 web/dist，由服务端托管
```

### 3. 建库建表

```bash
cd backend
cp .env.example .env                        # 可选：要改配置或接飞书时再编辑 .env
../.venv/bin/python cli.py db_init
```

`db_init` 先连 `PC_PG_DSN` 指定的库；库不存在时连同一服务器的 `postgres` 库执行 `CREATE DATABASE` 把它建出来（标准错误输出打印「数据库 procurement 不存在，已创建」），再按 `refdata/schema.sql` 建表。可以反复执行，已有的表和数据不受影响。

### 4. 启动服务，建第一个用户

```bash
../.venv/bin/python cli.py serve            # 仍在 backend/ 里；监听 http://127.0.0.1:8790，含后台调度，Ctrl+C 停止
```

浏览器打开 <http://127.0.0.1:8790>。系统里还没有用户时会进入「首次设置」（`/setup`）：填工作区名称、用户名和密码（至少 8 位），这个用户就是部署所有者。

也可以在命令行建用户（另开一个终端，在 `backend/` 里）：

```bash
../.venv/bin/python cli.py user_create -p username=demo -p workspace="我的工作区"
```

密码按提示输入两次，不回显；不要用 `-p password=…`，那样会留在 shell 历史和进程列表里。`-p workspace_id=1` 表示加入已有的工作区，不给则新建一个。

检查服务：`curl http://127.0.0.1:8790/healthz` 应返回 `{"ok":true}`。

## 配置：`backend/.env`

服务端的全部配置都是环境变量，可以写在 `backend/.env` 里（样例与缺省值见 [`backend/.env.example`](backend/.env.example)）：

- `registry/settings.py` 在进程第一次读设置时加载 `backend/.env`，之后不再重读：改了 `.env` 要重启 `cli.py serve`。
- 进程环境里已经有的变量优先，`.env` 不会覆盖它。
- 每行一个 `KEY=VALUE`，`#` 开头的行是注释；行尾不能写注释（会被当成值的一部分）。不改的项保持注释掉：连接串、地址、目录这类文本项写成 `PC_HOST=` 这样的空值时会被当成空字符串，不会回落到缺省值。
- `backend/.env` 已在 `.gitignore` 里，飞书 App Secret 只写在这里或进程环境里。

常用的几项：

|变量|缺省|说明|
|---|---|---|
|`PC_PG_DSN`|`dbname=procurement`|PostgreSQL 连接串（libpq 格式或 `postgresql://` 网址）|
|`PC_HOST`、`PC_PORT`|`127.0.0.1`、`8790`|监听地址与端口；改端口后插件设置页的服务端地址也要改|
|`PC_ALLOWED_HOSTS`|空|除本机地址外还接受的 Host 主机名，逗号分隔（防 DNS 重绑定）|
|`FEISHU_APP_ID`、`FEISHU_APP_SECRET`|空|飞书自建应用凭证；空着时控制台显示「未配置飞书应用」，文件导入不受影响|

其余（会话天数、租约、身份新鲜度、配对码有效期、限速、调度间隔、前端目录）见 `.env.example` 里的逐行说明。

## 接入飞书多维表格

不用飞书时可以跳过这一节，只用文件导入。

### 1. 配置飞书自建应用

1. 在飞书开放平台，用你有创建和发布权限的组织新建一个企业自建应用，在「凭证与基础信息」里拿到 App ID 与 App Secret。
2. 写进 `backend/.env`，然后重启 `cli.py serve`：

   ```bash
   FEISHU_APP_ID=cli_XXXXXXXXXXXXXXXX
   FEISHU_APP_SECRET=XXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXX
   ```

3. 在应用的「权限管理」里开通多维表格权限：只读接单需要能读数据表、字段和记录；要写回，再开通编辑多维表格记录的权限；要粘贴知识库（`/wiki/`）链接，还要能查看知识库节点。权限名称以开放平台上显示的为准。开通后创建版本并发布，权限在组织里生效后才能用。
4. 由表格管理者在目标多维表格右上角「…」→「更多」→「添加文档应用」里添加这个应用；要写回就给可编辑权限。表格开了高级权限时，再确认应用的角色看得到订单表和结果表（请求成功但读不到已知记录，不算连接成功）。

### 2. 在控制台连接表格

1. 控制台「订单来源」→「飞书连接」→「连接飞书表」。
2. 粘贴多维表格链接（右上角「分享」里复制），形如 `https://example.feishu.cn/base/XXXX?table=tblXXXX`；知识库链接也可以。链接只用来定位表格，凭证在服务端。真实链接只粘贴在控制台里，不要写进仓库。
3. 按向导走：粘贴链接 → 选择数据表（订单表与结果表）→ 字段对应 → 准入规则 → 样本预览（只读，不修改飞书数据）→ 保存。
4. 保存后服务端按连接的读取间隔定时读单，连接卡片上也可以「立即同步」；新单按账号配置自动分配。

### 3. 写回与验收

- 写回在向导的「字段对应」一步打开（「启用写回」并选好结果列）。打开后、验收通过前，写回队列只积压不发送，界面显示「回填待验证」。
- 验收：在连接卡片上点「写回验收」，填结果表里一条**专用测试记录**的记录 ID（形如 `recXXXX`）。服务端写入测试值、读回比对，再恢复原值；一致才开始真正写回。已经关联采购任务的记录会被拒绝，不能拿真实订单做验收。原值没能恢复时卡片上会留着提醒，需要到飞书里把那条记录手动改回原值。
- 模拟结果永不写回飞书。

### 4. 给其他工作区授权表格

部署所有者的工作区（第一个工作区）第一次连接某个多维表格时自动登记授权。其他工作区要用某个表格，由部署所有者在服务器上授权，一个表格同一时刻只归一个工作区：

```bash
cd backend
../.venv/bin/python cli.py feishu_grant                                        # 列出全部授权
../.venv/bin/python cli.py feishu_grant -p workspace_id=2 -p app_token=XXXX      # 授权给工作区 2
../.venv/bin/python cli.py feishu_grant -p workspace_id=2 -p link="https://example.feishu.cn/base/XXXX?table=tblXXXX"
../.venv/bin/python cli.py feishu_grant -p workspace_id=2 -p app_token=XXXX -p transfer=1   # 从别的工作区转过来
../.venv/bin/python cli.py feishu_grant -p revoke=1 -p app_token=XXXX            # 撤销
```

`app_token` 是多维表格链接里 `/base/` 后面那一段。知识库链接请先在控制台里解析出 `app_token` 再授权。

## 导入文件

1. 控制台「订单来源」→「文件导入」→「下载标准模板」（28 列，列名与飞书统一，只有「平台订单号」必填）。`reference/spreadsheets/` 里的是第三方参考系统的 15 列模板，不是本系统的标准模板。
2. 上传 `.xlsx` 或 `.csv`（UTF-8、UTF-8-BOM 或 GBK，10 MB 以内；`.xls` 请先另存为 `.xlsx`）。
3. 选工作表、核对字段对应（表头自动识别，可以存为个人模板，下次同样格式的文件自动套用）。
4. 预览：每行一个结论（新单、更新、无变化、待核实、历史采购、无效）并列出问题；改了字段对应要重新预览。
5. 「提交导入」或「放弃」。提交后新单按账号配置自动分配；问题行可以下载。

缺人审结论的任务停在「待确认」，缺字段的停在「资料待补齐」，都不会下发采购；建议采购日期在未来的，到那天才执行。

## 安装插件并模拟执行

先在控制台「账号与设备」→「账号」里「新建账号」（金额区间、每日最多单数、执行模式等），再导入订单，任务才分得出去。插件的完整说明见 [`extension/README.md`](extension/README.md)。

### 1. 构建

在仓库根目录：

```bash
cd extension && npm ci && npm run build     # 产物在 extension/dist
```

### 2. 装到本机 Chrome

1. 地址栏打开 `chrome://extensions`，打开右上角的「开发者模式」。
2. 点「加载已解压的扩展程序」，选择 `extension/dist` 文件夹（不是 `extension` 本身）。
3. 首次安装会自动打开插件设置页；之后也可以点工具栏的插件图标 →「打开设置」。

代码更新后重新 `npm run build`，在 `chrome://extensions` 里点插件卡片上的刷新按钮，再刷新已打开的亚马逊页面。

### 3. 装到 AdsPower

把同一个 `extension/dist` 文件夹作为自定义扩展加载到要用的浏览器环境里；需要上传压缩包时，用 `cd extension && npm run pack` 打出的 `extension/release/采购助手-0.1.0.zip`。**AdsPower 里的具体菜单路径还没有核实过**，以客户端里实际看到的为准。每个环境单独配对、单独起设备名称。

### 4. 配对

1. 控制台「账号与设备」→「设备」→「生成配对码」（8 位，10 分钟内有效，只能用一次）。
2. 插件设置页填：服务端地址 `http://127.0.0.1:8790`（改过 `PC_PORT` 就换成对应端口）、配对码、设备名称（如 `Mac-AdsPower-01`）、浏览器，点「配对」。
3. 配对失败时设置页写明原因（配对码无效或过期、尝试次数过多、服务端不接受该主机名、连不上服务端），对应处理见插件说明。

### 5. 绑定买家账号，核对身份

1. 控制台「设备」页里，在这个设备的「绑定账号」一栏选买家账号。几秒内插件面板就显示绑定的账号和服务端下发的配置。
2. 在这个浏览器里登录该亚马逊买家账号，打开任意 `www.amazon.com` 页面。插件读登录状态与买家号 ID 上报服务端，身份状态变成「身份一致」后才能开始执行。
3. 账号还没登记买家号 ID 时显示「身份未知」：在控制台「设备」页点「采用此 ID」。
4. 身份要在 10 分钟内上报过；开着亚马逊页面时插件会自动刷新，面板上也可以点「刷新身份」。

### 6. 开始模拟执行

亚马逊订单历史页上的面板默认展开，其他亚马逊页面右下角是收起的悬浮按钮。点「开始执行（模拟）」：插件领取分配给本账号、已到采购日期的任务，逐笔走护栏、提交登记、结果上报。成功的任务变成「已采购」，单号形如 `SIM-20261106-1`；账号执行模式为「定时执行」时，到了控制台配置的执行时间（北京时间）自动开始。插件设置页可以选模拟结局（超限价、缺货、验证码、提交后结果不明等），用来验证服务端的各条处理路径。

### 7. 清除模拟结果

控制台「设置」→「清除模拟结果」：所有模拟尝试标为已中止，受影响的任务按来源数据回到采购开始前的状态，释放模拟占用的额度，被模拟结果暂停的账号恢复执行。

## 命令

### 测试

在仓库根目录：

```bash
(cd backend && ../.venv/bin/python -m pytest)    # 需要本机 PostgreSQL：每次建一个临时库、结束时删除
(cd web && npm run build && npm test)            # vue-tsc 类型检查 + vite build + vitest
(cd extension && npm run build && npm test)      # tsc + vite build + Node 单测
```

后端测试的管理连接缺省是 `dbname=postgres`，要改时设进程环境变量 `PC_TEST_ADMIN_DSN`（写在 `.env` 里无效）。

### `backend/cli.py`

在 `backend/` 里用 `../.venv/bin/python cli.py <命令> [-p key=value ...]` 执行：

|命令|作用|
|---|---|
|`db_init`|库不存在时先建库，再建表（幂等）；`-p dsn=…` 临时指定连接串|
|`user_create`|建控制台用户：`-p username=…`，`-p workspace="名称"`（新建工作区）或 `-p workspace_id=…`（加入已有工作区）；密码按提示输入|
|`serve`|HTTP 服务 + 后台调度（清扫、每日分配、飞书读取与写回）；可选 `-p host=…`、`-p port=…`、`-p jobs=0`（不启动后台调度）、`-p log_level=…`|
|`allocate`|账号分配，等同控制台「重新分配」；`-p workspace_id=…` 只分一个工作区|
|`sweep`|清扫过期的执行批次租约|
|`feishu_intake`|读一次到期的飞书连接；`-p connection_id=…` 只读这一个|
|`feishu_writeback`|发送一轮到期的飞书写回|
|`feishu_grant`|把多维表格授权给工作区，或列出、转授、撤销授权（见上文）|

后台调度跑的就是这些命令里的同一套代码，单独执行与后台执行是同一件事。

## 目录

```
backend/
  cli.py  scheduler.py    命令入口；serve = HTTP 服务 + 后台调度
  server/                 FastAPI 应用与路由（参数校验、权限、事务边界、JSON 转换）
  workflows/              cli 命令的实现（db_init、user_create、allocate、sweep、飞书读取 / 写回 / 授权）
  services/               业务规则（状态机、分配、护栏、执行协议、来源接入、写回）
  api/                    飞书开放平台适配（认证、重试、分页、限速）
  registry/               配置（settings.py）、路径、数据库连接
  refdata/schema.sql      表结构
  tests/                  pytest，真实 PostgreSQL
  .env.example            配置样例
web/                      控制台前端（构建产物 web/dist 由服务端托管）
extension/                浏览器插件（构建产物 extension/dist）
docs/development/         方案与开发契约
docs/research/            调研报告（只读）
reference/                参考插件与表格模板（只读）
```

## 文档

|文档|内容|
|---|---|
|[采购控制台方案 v2](docs/development/采购控制台方案-v2.md)|所有者定稿、在 v1 之上的取舍、第一个 PR 的范围与验收；与 v1 冲突处以 v2 为准|
|[采购与订单同步系统落地方案 v1](docs/development/采购与订单同步系统落地方案-v1.md)|业务规则全集：订单来源、多人隔离、账号分配、限价、异常及物流同步|
|[开发契约 v2](docs/development/开发契约-v2.md)|实现契约：表、状态机、分配、护栏、插件执行协议、HTTP 接口与错误码|
|[CLAUDE.md](CLAUDE.md)|分层规则与开发约定|

## 参考材料

只读资料，开发时参考，不要修改。

|内容|入口|
|---|---|
|开发方案：订单来源、多人隔离、账号分配、限价、异常及物流同步|[落地方案](docs/development/采购与订单同步系统落地方案-v1.md)|
|前端与自有浏览器插件共用的界面模板|[指定 Claude Artifact](https://claude.ai/artifact/79rovitE1iBjEJ1xwhN8qP)|
|调研报告、接口与字段、证据来源|[逆向分析](docs/research/小蜜蜂逆向分析报告.md) · [接口附录](docs/research/接口与字段附录.md) · [证据索引](docs/research/证据索引.md)|
|第三方参考扩展的原版运行文件|[参考插件说明](reference/README.md) · [2.5.5 源码](reference/smallbee-2.5.5)|
|采购导入与订单物流导出 Excel|[下载表头模板及列序说明](reference/spreadsheets/README.md)|

文中的演示标识不能用于查询真实订单。表格只含表头；私人飞书链接、真实订单、原始物流截图、支付及收件信息未公开。仓库是公开的：测试数据一律虚构，真实的飞书链接、表格 token、订单、收件人和卡号不进仓库。
