# 采购控制台 · 项目总纲

自用的订单自动采购系统：订单来自飞书多维表格或手动上传的表格，分配给亚马逊买家账号，由对应浏览器里的自有插件领取执行（第一个 PR 只做模拟执行）。

- 业务定稿：`docs/development/采购控制台方案-v2.md`（与 v1 冲突处以 v2 为准）；业务规则全集：`docs/development/采购与订单同步系统落地方案-v1.md`。
- 实现契约（表、状态机、分配、护栏、执行协议、HTTP 接口）：`docs/development/开发契约-v2.md`。**改契约就要同时改代码和测试。**
- 表结构：`backend/refdata/schema.sql`。
- 参考材料（只读，不要修改）：`reference/`、`docs/research/`。

## 目录

```
backend/   Python 服务端（FastAPI + PostgreSQL + psycopg 3，裸 SQL，无 ORM）
web/       控制台前端（Vue 3 + TypeScript + Vite + Element Plus 2.14 + Tailwind 3，EasyQuick 外观）
extension/ 浏览器插件（Chrome MV3 + TypeScript + Vite；AdsPower 与 Chrome 均可加载）
```

## 常用命令

```bash
python3 -m venv .venv && .venv/bin/pip install -r backend/requirements.txt   # 在仓库根目录装后端依赖（含 pytest）
source .venv/bin/activate

cd backend
python -m pytest -q                 # 后端测试（需要本机 PostgreSQL；PC_TEST_ADMIN_DSN 可改管理连接）
python cli.py db_init               # 库不存在时先建库，再建表（幂等）
python cli.py user_create -p username=… -p workspace=…   # 建用户与工作区（密码交互输入）
python cli.py serve                 # 127.0.0.1:8790，托管 web/dist，含后台调度
python cli.py allocate              # 后台任务单跑：allocate / sweep / feishu_intake / feishu_writeback
python cli.py feishu_grant -p workspace_id=… -p app_token=…   # 把飞书多维表格授权给工作区

cd web && npm run build && npm test # 前端类型检查 + 构建 + 单测
cd extension && npm run build && npm test
```

配置取进程环境变量或 `backend/.env`，变量与缺省值见契约 §0。

## 后端铁律

1. **依赖只准自上而下**：`cli.py / scheduler.py → (server | workflows) → services → api → registry`。`server` 与 `workflows` 互不导入；`api` 不导入 `services`；任何层不导入入口层。
2. **业务判断只在 services**。`api/` 只做外部接口适配（认证、重试、分页、限速）；`server/routes` 只做参数校验、权限、事务边界和 JSON 转换。
3. **路径、DSN、端口、密钥、可调参数只从 `registry/` 取**；数据库连接只经 `registry/db.pg_conn()`。
4. **services 不 commit**：函数接收调用方的 `conn`，事务边界在入口层（`with db.pg_conn() as conn:` 正常退出即提交）。
5. **每个查询都带工作区条件**；跨工作区访问一律 404（例外只有契约 §0 列出的认证查询、后台任务挑选到期行与 `feishu_base_grants`）。
6. **时间只经 `services/clock`**：业务日期是北京日期（`bj_today()`），配送天数按站点日期（`site_today()`）。测试用 `clock.freeze()`。
7. **NULL 不是 0**：没读到的金额、天数、日期保持 None，走「资料待补齐」或护栏拦截，绝不当 0 放行。
8. **任务状态只经 `services/orders.transition()` 改**，它校验迁移表并写事件；唯一例外是清除模拟结果的 `orders.revert_simulation()`（直接退回开始前的派生状态，照样写 `order.status_changed`）。
9. **登记提交后绝不自动重买**：`submitted_at` 非空的尝试，任何非成功结局都进「结果待核实」并暂停账号。
10. **模拟结果永不写回飞书**；本阶段服务端拒绝非模拟执行批次。
11. 密钥永不进 git：飞书 App ID / Secret 只在 `backend/.env` 或进程环境。仓库是公开的：**不得提交真实飞书链接、表 token、订单、收件人、卡号**；测试数据一律虚构。

## 前端与插件约定

- 外观以 EasyQuick 设计系统为准：`web/src/design-system/`（tokens、Element Plus 主题、样式表、图标）。交互用 Element Plus，外壳与展示部件（侧边栏、顶栏、页面标题、统计卡、空状态、状态点）用本仓库的 Vue 组件。图标只用 `AppIcon`。
- 文案：简体中文，中英文之间留半角空格，不用感叹号和 emoji；按钮用动词；取消按钮叫「取消」；确认框「确定要……吗？」；失败提示「动作 + 失败：原因」。
- 插件不申请 `cookies` 权限，不上传页面 HTML，不做任何反检测；遇验证码转人工。

## 测试

- 后端：pytest，真实 PostgreSQL；`tests/conftest.py` 每个会话建临时库，每个测试前清空。每个服务模块都要有测试，v2 方案 §4 的验收条目必须有对应用例。
- 前端：`vue-tsc --noEmit` + `vite build` 必须通过；纯函数用 vitest。
- 插件：TypeScript 编译 + Node 单测（纯函数与协议客户端）。
