-- 采购控制台建表脚本（幂等，db_init 执行）。契约见 docs/development/开发契约-v2.md。
-- PostgreSQL 16 / 17。所有时间为 timestamptz（UTC）；业务日期为北京日期，由应用层计算后写入 date 列。
--
-- 新库：CREATE TABLE 直接是当前形状。旧库：CREATE TABLE IF NOT EXISTS 不动已有的表，靠文件后面各段
-- 「追加（幂等）」的 ALTER / DO 块升级；整份脚本可以在新库、旧库上反复执行。
-- 升级顺序：停服务 → python cli.py db_init → 启动 → 在控制台点一次「重新分配」→ 重新加载插件。

CREATE SCHEMA IF NOT EXISTS procure;

-- ── 身份 ──────────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS procure.workspaces (
    id          bigserial PRIMARY KEY,
    name        text NOT NULL,
    created_at  timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS procure.users (
    id             bigserial PRIMARY KEY,
    username       text NOT NULL UNIQUE,
    display_name   text,
    password_hash  text NOT NULL,           -- scrypt$<n>$<r>$<p>$<salt_b64>$<hash_b64>
    created_at     timestamptz NOT NULL DEFAULT now(),
    disabled_at    timestamptz
);

CREATE TABLE IF NOT EXISTS procure.workspace_members (
    workspace_id  bigint NOT NULL REFERENCES procure.workspaces(id),
    user_id       bigint NOT NULL REFERENCES procure.users(id),
    role          text NOT NULL DEFAULT 'owner' CHECK (role IN ('owner', 'member')),
    created_at    timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (workspace_id, user_id)
);

CREATE TABLE IF NOT EXISTS procure.sessions (
    token_hash    text PRIMARY KEY,          -- sha256(token) hex
    user_id       bigint NOT NULL REFERENCES procure.users(id),
    workspace_id  bigint NOT NULL REFERENCES procure.workspaces(id),
    created_at    timestamptz NOT NULL DEFAULT now(),
    expires_at    timestamptz NOT NULL,
    last_seen_at  timestamptz NOT NULL DEFAULT now(),
    revoked_at    timestamptz
);

CREATE TABLE IF NOT EXISTS procure.workspace_settings (
    workspace_id               bigint PRIMARY KEY REFERENCES procure.workspaces(id),
    default_max_delivery_days  integer NOT NULL DEFAULT 10 CHECK (default_max_delivery_days BETWEEN 1 AND 60),
    simulation_only            boolean NOT NULL DEFAULT true,   -- 第一个 PR：只接受模拟执行
    logistics_sync_time        text NOT NULL DEFAULT '08:30',   -- 第二个 PR 使用（北京时间 HH:MM）
    updated_at                 timestamptz NOT NULL DEFAULT now(),
    updated_by                 bigint REFERENCES procure.users(id)
);

-- ── 买家账号与浏览器（设备）──────────────────────────────────────────────────────
-- 买家号的身份 = 亚马逊 customerId（买家号 ID）。账号由所有者在控制台登记；浏览器（devices 行）由插件自动登记，
-- 记下它最近上报的登录状态。任何登录着某个买家号的浏览器都能执行登记了这个 ID 的账号的任务，两者之间没有绑定。

CREATE TABLE IF NOT EXISTS procure.buyer_accounts (
    id                   bigserial PRIMARY KEY,
    workspace_id         bigint NOT NULL REFERENCES procure.workspaces(id),
    name                 text NOT NULL,                       -- 与飞书「买家号」取值一致，如 环境-DEMO-A
    amazon_customer_id   text CHECK (amazon_customer_id IS NULL OR amazon_customer_id ~ '^A[0-9A-Z]{8,}$'),
    site                 text NOT NULL DEFAULT 'US' CHECK (site IN ('US')),
    min_amount           numeric(12,2) NOT NULL DEFAULT 0 CHECK (min_amount >= 0),
    max_amount           numeric(12,2) CHECK (max_amount IS NULL OR max_amount >= min_amount),
    daily_limit          integer NOT NULL DEFAULT 20 CHECK (daily_limit >= 0),
    priority             smallint NOT NULL DEFAULT 2 CHECK (priority BETWEEN 0 AND 9),
    max_delivery_days    integer CHECK (max_delivery_days IS NULL OR max_delivery_days BETWEEN 1 AND 60),  -- NULL = 继承全局
    purchase_enabled     boolean NOT NULL DEFAULT true,       -- 关闭 = 不再接收新分配
    sync_enabled         boolean NOT NULL DEFAULT true,       -- 订单与物流同步（第二个 PR）
    execution_paused     boolean NOT NULL DEFAULT false,      -- 暂停执行（与采购开关分开）
    pause_reason         text,
    paused_at            timestamptz,
    purchase_mode        text NOT NULL DEFAULT 'manual' CHECK (purchase_mode IN ('manual', 'scheduled')),
    schedule_times       text[] NOT NULL DEFAULT '{}',        -- 北京时间 'HH:MM'
    expected_card_last4  text CHECK (expected_card_last4 IS NULL OR expected_card_last4 ~ '^[0-9]{4}$'),
    notes                text,
    created_at           timestamptz NOT NULL DEFAULT now(),
    updated_at           timestamptz NOT NULL DEFAULT now(),
    archived_at          timestamptz,
    UNIQUE (workspace_id, name)
);

CREATE TABLE IF NOT EXISTS procure.devices (
    id                 bigserial PRIMARY KEY,
    workspace_id       bigint NOT NULL REFERENCES procure.workspaces(id),  -- 登记时确定，之后不变
    name               text NOT NULL,                         -- 「浏览器 <id>」，之后只能在控制台改名
    browser            text,                                  -- 历史列（旧版插件上报的 chrome / adspower / other），不再写入
    token_hash         text UNIQUE,                           -- sha256(device_token) hex；令牌只是会话标识，不代表身份
    ext_version        text,
    user_agent         text,
    last_seen_at       timestamptz,
    identity           jsonb,                                 -- {signed_in, customer_id, site, page_url, conflict, at}
    identity_at        timestamptz,
    login_customer_id  text,                                  -- 已登录、读到合格 ID 且各标签页读数一致时的买家号 ID，否则 NULL
    created_at         timestamptz NOT NULL DEFAULT now(),
    hidden_at          timestamptz                            -- 控制台「移除」的时间；浏览器再次联系时清空
);

-- 所有者选择忽略的买家号 ID（不出现在「待登记的买家号」里；登记成账号时删掉对应的行）
CREATE TABLE IF NOT EXISTS procure.ignored_buyer_ids (
    workspace_id  bigint NOT NULL REFERENCES procure.workspaces(id),
    customer_id   text NOT NULL,
    created_by    bigint REFERENCES procure.users(id),
    created_at    timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (workspace_id, customer_id)
);

-- ── 订单来源 ──────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS procure.feishu_connections (
    id                    bigserial PRIMARY KEY,
    workspace_id          bigint NOT NULL REFERENCES procure.workspaces(id),
    name                  text NOT NULL,
    link                  text NOT NULL,                      -- 用户粘贴的链接（只入库，不入仓库）
    app_token             text NOT NULL,
    source_table_id       text NOT NULL,
    source_view_id        text,
    result_table_id       text NOT NULL,                      -- 写回所在表（默认同 source_table_id）
    field_map             jsonb NOT NULL DEFAULT '{}',        -- {系统字段: 飞书列名}
    admission             jsonb NOT NULL DEFAULT '{}',        -- 准入规则，见契约 §6
    writeback_map         jsonb NOT NULL DEFAULT '{}',        -- {结果字段: 飞书列名}
    writeback_enabled     boolean NOT NULL DEFAULT false,
    writeback_verified_at timestamptz,                        -- 测试记录验收通过时间；NULL = 回填待验证
    writeback_test_record text,
    poll_minutes          integer NOT NULL DEFAULT 10 CHECK (poll_minutes BETWEEN 1 AND 1440),
    enabled               boolean NOT NULL DEFAULT true,
    last_sync_at          timestamptz,
    last_sync_ok_at       timestamptz,
    last_error            text,
    last_stats            jsonb,
    created_by            bigint REFERENCES procure.users(id),
    created_at            timestamptz NOT NULL DEFAULT now(),
    updated_at            timestamptz NOT NULL DEFAULT now(),
    UNIQUE (workspace_id, app_token, source_table_id)
);

CREATE TABLE IF NOT EXISTS procure.feishu_sync_runs (
    id             bigserial PRIMARY KEY,
    workspace_id   bigint NOT NULL REFERENCES procure.workspaces(id),
    connection_id  bigint NOT NULL REFERENCES procure.feishu_connections(id),
    trigger        text NOT NULL CHECK (trigger IN ('schedule', 'manual')),
    started_at     timestamptz NOT NULL DEFAULT now(),
    finished_at    timestamptz,
    ok             boolean,
    stats          jsonb,                                     -- {read, new, update, unchanged, conflict, historical, skipped, invalid}
    error          text
);

CREATE TABLE IF NOT EXISTS procure.import_templates (
    id            bigserial PRIMARY KEY,
    workspace_id  bigint NOT NULL REFERENCES procure.workspaces(id),
    name          text NOT NULL,
    mapping       jsonb NOT NULL,                             -- {文件表头: 系统字段}
    created_by    bigint REFERENCES procure.users(id),
    created_at    timestamptz NOT NULL DEFAULT now(),
    UNIQUE (workspace_id, name)
);

CREATE TABLE IF NOT EXISTS procure.import_batches (
    id            bigserial PRIMARY KEY,
    workspace_id  bigint NOT NULL REFERENCES procure.workspaces(id),
    filename      text NOT NULL,
    file_sha256   text NOT NULL,
    file_bytes    bytea NOT NULL,                             -- 上传原件（只读副本，用于重新预览）
    sheet_name    text,
    mapping       jsonb,
    status        text NOT NULL DEFAULT 'uploaded' CHECK (status IN ('uploaded', 'previewed', 'committed', 'discarded')),
    stats         jsonb,
    uploaded_by   bigint REFERENCES procure.users(id),
    created_at    timestamptz NOT NULL DEFAULT now(),
    committed_at  timestamptz,
    committed_by  bigint REFERENCES procure.users(id)
);

CREATE TABLE IF NOT EXISTS procure.import_rows (
    batch_id           bigint NOT NULL REFERENCES procure.import_batches(id) ON DELETE CASCADE,
    row_no             integer NOT NULL,                      -- 文件中的行号（表头为第 1 行）
    raw                jsonb NOT NULL,                        -- {表头: 原值}
    normalized         jsonb,                                 -- UpstreamRecord
    verdict            text NOT NULL CHECK (verdict IN ('new', 'update', 'unchanged', 'conflict', 'historical', 'invalid')),
    issues             text[] NOT NULL DEFAULT '{}',
    purchase_order_id  bigint,
    PRIMARY KEY (batch_id, row_no)
);

-- ── 采购任务 ──────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS procure.purchase_orders (
    id                       bigserial PRIMARY KEY,
    workspace_id             bigint NOT NULL REFERENCES procure.workspaces(id),
    upstream_order_no        text NOT NULL,                   -- 平台订单号（上游采购单唯一键）
    order_line_id            text,
    store_name               text,
    sales_po_no              text,
    sku                      text,
    asin                     text,
    quantity                 integer,
    unit_price               numeric(12,2),                   -- 采购单价（审核时的亚马逊单价）
    sales_total              numeric(12,2),
    price_cap                numeric(12,2),                   -- 上游限价（原值）
    order_time               timestamptz,
    order_date_bj            date,
    ship_days                integer,
    suggested_purchase_date  date,
    suggested_date_basis     text CHECK (suggested_date_basis IN ('source', 'computed')),
    ship_method              text,
    recipient_name           text,
    address1                 text,
    address2                 text,
    city                     text,
    state                    text,
    postal_code              text,
    phone                    text,
    country                  text NOT NULL DEFAULT 'US',
    district                 text,
    review_status            text NOT NULL DEFAULT 'unknown' CHECK (review_status IN ('approved', 'rejected', 'unknown')),
    review_source            text CHECK (review_source IN ('feishu', 'file', 'console')),
    review_value             text,
    reviewed_by              bigint REFERENCES procure.users(id),
    reviewed_at              timestamptz,
    upstream_cancelled       boolean NOT NULL DEFAULT false,
    upstream_cancel_note     text,
    upstream_cancelled_at    timestamptz,
    external_amz_order_no    text,                            -- 来源已带的 AMZ 订单号（历史 / 外部采购）
    -- 控制台覆盖值：只作用于采购，不回填来源，后续导入不得覆盖
    override_asin            text,
    override_quantity        integer CHECK (override_quantity IS NULL OR override_quantity > 0),
    override_price_cap       numeric(12,2) CHECK (override_price_cap IS NULL OR override_price_cap > 0),
    override_note            text,
    override_by              bigint REFERENCES procure.users(id),
    override_at              timestamptz,
    status                   text NOT NULL CHECK (status IN (
                               'pending_review', 'needs_info', 'conflict', 'queued', 'assigned',
                               'executing', 'result_unknown', 'exception', 'purchased',
                               'cancelled', 'repurchase_pending', 'closed')),
    status_reason            text,
    status_changed_at        timestamptz NOT NULL DEFAULT now(),
    missing_fields           text[] NOT NULL DEFAULT '{}',
    conflict                 jsonb,                           -- {kind, source_kind, reason, fields, incoming, source, at}（契约 §2）
    needs_attention          boolean NOT NULL DEFAULT false,
    attention_note           text,
    primary_source_kind      text NOT NULL CHECK (primary_source_kind IN ('feishu', 'file')),
    assigned_account_id      bigint REFERENCES procure.buyer_accounts(id),
    assignment_kind          text CHECK (assignment_kind IN ('auto', 'manual', 'source')),  -- source = 按来源指定的买家号
    assigned_at              timestamptz,
    assignment_note          text,
    -- 来源指定的买家号（清洗后的值；都为 NULL = 没指定，由系统自动分配）。不加形状约束：格式不对的值也要能存下，
    -- 由分配给出「待分配」的原因。解除分配、同意重采、清除模拟结果都不动这两列。
    designated_customer_id   text,
    designated_buyer_name    text,
    page_load_retries        integer NOT NULL DEFAULT 0,      -- 提交前页面错误的自动重试次数
    current_attempt_id       bigint,
    purchased_attempt_id     bigint,
    result_version           integer NOT NULL DEFAULT 0,      -- 结果版本（写回去重）
    created_at               timestamptz NOT NULL DEFAULT now(),
    updated_at               timestamptz NOT NULL DEFAULT now(),
    UNIQUE (workspace_id, upstream_order_no),
    CHECK ((assigned_account_id IS NULL) = (assignment_kind IS NULL))
);

CREATE INDEX IF NOT EXISTS purchase_orders_ws_status ON procure.purchase_orders (workspace_id, status);
CREATE INDEX IF NOT EXISTS purchase_orders_account ON procure.purchase_orders (assigned_account_id, status);

CREATE TABLE IF NOT EXISTS procure.order_sources (
    id                    bigserial PRIMARY KEY,
    workspace_id          bigint NOT NULL REFERENCES procure.workspaces(id),
    purchase_order_id     bigint NOT NULL REFERENCES procure.purchase_orders(id),
    kind                  text NOT NULL CHECK (kind IN ('feishu', 'file')),
    feishu_connection_id  bigint REFERENCES procure.feishu_connections(id),
    feishu_record_id      text,
    import_batch_id       bigint REFERENCES procure.import_batches(id),
    import_row_no         integer,
    snapshot              jsonb NOT NULL,                     -- 来源原始字段 → 值
    normalized            jsonb NOT NULL,                     -- UpstreamRecord
    fingerprint           text NOT NULL,                      -- sha256(规范化后的执行相关字段)
    is_writeback_target   boolean NOT NULL DEFAULT false,
    first_seen_at         timestamptz NOT NULL DEFAULT now(),
    last_seen_at          timestamptz NOT NULL DEFAULT now(),
    CHECK ((kind = 'feishu' AND feishu_connection_id IS NOT NULL AND feishu_record_id IS NOT NULL)
        OR (kind = 'file' AND import_batch_id IS NOT NULL AND import_row_no IS NOT NULL))
);

CREATE UNIQUE INDEX IF NOT EXISTS order_sources_feishu_uq
    ON procure.order_sources (feishu_connection_id, feishu_record_id) WHERE kind = 'feishu';
CREATE UNIQUE INDEX IF NOT EXISTS order_sources_file_uq
    ON procure.order_sources (import_batch_id, import_row_no) WHERE kind = 'file';
CREATE UNIQUE INDEX IF NOT EXISTS order_sources_one_writeback_target
    ON procure.order_sources (purchase_order_id) WHERE is_writeback_target;
CREATE INDEX IF NOT EXISTS order_sources_order ON procure.order_sources (purchase_order_id);

-- ── 执行 ──────────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS procure.execution_runs (
    id                bigserial PRIMARY KEY,
    workspace_id      bigint NOT NULL REFERENCES procure.workspaces(id),
    account_id        bigint NOT NULL REFERENCES procure.buyer_accounts(id),
    device_id         bigint NOT NULL REFERENCES procure.devices(id),
    trigger           text NOT NULL CHECK (trigger IN ('manual', 'scheduled')),
    simulated         boolean NOT NULL,
    started_at        timestamptz NOT NULL DEFAULT now(),
    heartbeat_at      timestamptz NOT NULL DEFAULT now(),
    lease_expires_at  timestamptz NOT NULL,
    ended_at          timestamptz,
    end_reason        text,                                   -- finished / lease_expired / simulation_reset / identity_mismatch（历史行可能有 device_revoked）
    stats             jsonb NOT NULL DEFAULT '{}',
    bj_date           date NOT NULL,                          -- 开始批次时的北京日期（定时时间点按它去重）
    scheduled_slot    text CHECK (scheduled_slot IS NULL OR scheduled_slot ~ '^([01][0-9]|2[0-3]):[0-5][0-9]$')
                                                              -- 定时批次对应的时间点（北京时间 'HH:MM'）；手动批次为 NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS execution_runs_one_active_per_account
    ON procure.execution_runs (account_id) WHERE ended_at IS NULL;

CREATE TABLE IF NOT EXISTS procure.purchase_attempts (
    id                 bigserial PRIMARY KEY,
    workspace_id       bigint NOT NULL REFERENCES procure.workspaces(id),
    purchase_order_id  bigint NOT NULL REFERENCES procure.purchase_orders(id),
    attempt_no         integer NOT NULL,
    kind               text NOT NULL CHECK (kind IN ('auto', 'retry', 'repurchase', 'external')),
    account_id         bigint REFERENCES procure.buyer_accounts(id),
    device_id          bigint REFERENCES procure.devices(id),
    run_id             bigint REFERENCES procure.execution_runs(id),
    simulated          boolean NOT NULL DEFAULT false,
    status             text NOT NULL CHECK (status IN (
                         'claimed', 'checking', 'guard_passed', 'submitting', 'verifying',
                         'succeeded', 'failed', 'unknown', 'aborted')),
    bj_date            date NOT NULL,                         -- 计额度的北京日期（尝试创建当天）
    stage              text,
    claimed_at         timestamptz NOT NULL DEFAULT now(),
    heartbeat_at       timestamptz,
    submitted_at       timestamptz,                           -- 提交登记时间：非空即「可能已下单」
    finished_at        timestamptz,
    task_snapshot      jsonb,                                 -- 下发给插件的执行参数
    guard_input        jsonb,
    guard_verdict      jsonb,
    amazon_order_no    text,
    buyer_account_name text,                                  -- 外部尝试：来源中的买家号
    subtotal           numeric(12,2),
    shipping           numeric(12,2),
    tax                numeric(12,2),
    total_before_tax   numeric(12,2),
    total              numeric(12,2),
    currency           text NOT NULL DEFAULT 'USD',
    card_last4         text,
    delivery_text      text,
    delivery_date      date,
    delivery_days      integer,
    error_code         text,
    error_detail       text,
    resolution         text,                                  -- 人工核实：ordered / not_ordered / simulation_reset
    resolution_note    text,
    resolved_by        bigint REFERENCES procure.users(id),
    resolved_at        timestamptz,
    UNIQUE (purchase_order_id, attempt_no)
);

CREATE UNIQUE INDEX IF NOT EXISTS purchase_attempts_one_inflight_per_order
    ON procure.purchase_attempts (purchase_order_id)
    WHERE status IN ('claimed', 'checking', 'guard_passed', 'submitting', 'verifying');
CREATE UNIQUE INDEX IF NOT EXISTS purchase_attempts_one_inflight_per_account
    ON procure.purchase_attempts (account_id)
    WHERE status IN ('claimed', 'checking', 'guard_passed', 'submitting', 'verifying');
CREATE UNIQUE INDEX IF NOT EXISTS purchase_attempts_amazon_order_uq
    ON procure.purchase_attempts (workspace_id, amazon_order_no)
    WHERE amazon_order_no IS NOT NULL AND NOT simulated AND status = 'succeeded';
CREATE INDEX IF NOT EXISTS purchase_attempts_quota
    ON procure.purchase_attempts (account_id, bj_date, simulated);

DO $$ BEGIN
    ALTER TABLE procure.purchase_orders
        ADD CONSTRAINT purchase_orders_current_attempt_fk
        FOREIGN KEY (current_attempt_id) REFERENCES procure.purchase_attempts(id);
EXCEPTION WHEN duplicate_object THEN NULL; END $$;
DO $$ BEGIN
    ALTER TABLE procure.purchase_orders
        ADD CONSTRAINT purchase_orders_purchased_attempt_fk
        FOREIGN KEY (purchased_attempt_id) REFERENCES procure.purchase_attempts(id);
EXCEPTION WHEN duplicate_object THEN NULL; END $$;
DO $$ BEGIN
    ALTER TABLE procure.import_rows
        ADD CONSTRAINT import_rows_order_fk
        FOREIGN KEY (purchase_order_id) REFERENCES procure.purchase_orders(id);
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

-- ── 写回、分配、事件 ──────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS procure.writebacks (
    id                 bigserial PRIMARY KEY,
    workspace_id       bigint NOT NULL REFERENCES procure.workspaces(id),
    purchase_order_id  bigint NOT NULL REFERENCES procure.purchase_orders(id),
    connection_id      bigint NOT NULL REFERENCES procure.feishu_connections(id),
    table_id           text NOT NULL,
    record_id          text NOT NULL,
    result_version     integer NOT NULL,
    fields             jsonb NOT NULL,                        -- {飞书列名: 值}
    status             text NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'done', 'failed', 'superseded')),
    tries              integer NOT NULL DEFAULT 0,
    last_error         text,
    next_try_at        timestamptz NOT NULL DEFAULT now(),
    created_at         timestamptz NOT NULL DEFAULT now(),
    done_at            timestamptz,
    UNIQUE (purchase_order_id, connection_id, result_version)
);

CREATE INDEX IF NOT EXISTS writebacks_pending ON procure.writebacks (status, next_try_at);

CREATE TABLE IF NOT EXISTS procure.allocation_runs (
    id            bigserial PRIMARY KEY,
    workspace_id  bigint NOT NULL REFERENCES procure.workspaces(id),
    trigger       text NOT NULL,                              -- intake / import / manual / account_change / daily
    bj_date       date NOT NULL,
    at            timestamptz NOT NULL DEFAULT now(),
    stats         jsonb NOT NULL,                             -- {considered, assigned, coverage, fill, designated, unassigned}
    uncovered     jsonb NOT NULL DEFAULT '[]',                -- [{account_id, name, date, reason}]
    unassigned    jsonb NOT NULL DEFAULT '[]'                 -- [{order_id, upstream_order_no, reason, designated?}]
);

CREATE TABLE IF NOT EXISTS procure.events (
    id            bigserial PRIMARY KEY,
    workspace_id  bigint NOT NULL REFERENCES procure.workspaces(id),
    entity        text NOT NULL CHECK (entity IN ('order', 'attempt', 'account', 'device', 'connection', 'batch', 'settings', 'run')),
    entity_id     bigint,
    order_id      bigint,                                     -- 便于按任务查全部相关事件
    at            timestamptz NOT NULL DEFAULT now(),
    actor_kind    text NOT NULL CHECK (actor_kind IN ('user', 'device', 'system', 'source')),
    actor_id      bigint,
    kind          text NOT NULL,
    detail        jsonb NOT NULL DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS events_entity ON procure.events (workspace_id, entity, entity_id, at DESC);
CREATE INDEX IF NOT EXISTS events_order ON procure.events (order_id, at DESC) WHERE order_id IS NOT NULL;

-- ── 订单来源模块追加（幂等）──────────────────────────────────────────────────────

-- 写回列的飞书字段类型 {飞书列名: 字段类型}：建连接 / 改写回对应 / 写回验收时从飞书读取，
-- 入队时按列类型转换写回值（数字列写数字、单选写选项文本），入队本身不访问飞书。
ALTER TABLE procure.feishu_connections ADD COLUMN IF NOT EXISTS writeback_field_types jsonb NOT NULL DEFAULT '{}';

-- ── 追加（幂等）：文件导入的准入规则、批次预览时用的准入规则 ──────────────────────────
-- workspace_settings.file_admission：工作区改过的文件准入键（缺省见 services/normalize.FILE_ADMISSION_DEFAULTS）。
-- import_batches.admission：预览时实际用的完整准入规则，提交时对已有任务做同样的准入检查。
ALTER TABLE procure.workspace_settings ADD COLUMN IF NOT EXISTS file_admission jsonb NOT NULL DEFAULT '{}'::jsonb;
ALTER TABLE procure.import_batches ADD COLUMN IF NOT EXISTS admission jsonb;

-- ── 追加（幂等）：import_rows 带工作区（契约 §0：每张业务表都有 workspace_id，查询都带工作区条件）────────────
ALTER TABLE procure.import_rows ADD COLUMN IF NOT EXISTS workspace_id bigint REFERENCES procure.workspaces(id);
UPDATE procure.import_rows r SET workspace_id = b.workspace_id
    FROM procure.import_batches b WHERE b.id = r.batch_id AND r.workspace_id IS NULL;
ALTER TABLE procure.import_rows ALTER COLUMN workspace_id SET NOT NULL;
CREATE INDEX IF NOT EXISTS import_rows_ws_batch ON procure.import_rows (workspace_id, batch_id);

-- ── 追加（幂等）：飞书多维表格的工作区授权（v1 §6.1 第 3 步：链接只用来定位，不等于授权）────────────
-- 一个多维表格（app_token，含其中全部数据表）同一时刻只归一个工作区。部署所有者的工作区（第一个工作区）
-- 绑定尚未授权的表格时自动登记；其他工作区必须由部署所有者在服务器上运行 python cli.py feishu_grant 授权。
-- 这是跨工作区的登记表（与 users 一样不按工作区分区），只在授权检查与 CLI 里读写。
CREATE TABLE IF NOT EXISTS procure.feishu_base_grants (
    app_token     text PRIMARY KEY,
    workspace_id  bigint NOT NULL REFERENCES procure.workspaces(id),
    granted_by    text NOT NULL CHECK (granted_by IN ('owner_workspace', 'cli', 'migrated')),
    granted_at    timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS feishu_base_grants_ws ON procure.feishu_base_grants (workspace_id);
-- 已有的启用连接：每个表格归最早连接它的工作区。撤销 / 转授时原工作区的连接会被停用，所以重复执行本句
-- 不会把撤销掉的授权又加回来。
INSERT INTO procure.feishu_base_grants (app_token, workspace_id, granted_by)
SELECT DISTINCT ON (app_token) app_token, workspace_id, 'migrated'
FROM procure.feishu_connections WHERE enabled ORDER BY app_token, id
ON CONFLICT (app_token) DO NOTHING;
-- 一个表格同一时刻只归一个工作区：表格已授权给别的工作区时，这里仍启用的同表连接一律停用（读取、验收、写回发送
-- 本来也会被拒绝；需要时由部署所有者用 CLI 转授后再启用）。幂等。
UPDATE procure.feishu_connections c SET enabled = false, updated_at = now()
WHERE c.enabled AND EXISTS (SELECT 1 FROM procure.feishu_base_grants g
                            WHERE g.app_token = c.app_token AND g.workspace_id <> c.workspace_id);

-- ── 追加（幂等）：同一工作区里同一个亚马逊买家号 ID 只能登记在一个未归档账号上 ──────────────────
-- 执行权互斥按账号行算：同一个亚马逊登录挂在两行上就可能两处同时执行、各占一份额度。应用层检查之外由唯一索引兜底
-- （两次并发的新建 / 修改也只成功一次）。
CREATE UNIQUE INDEX IF NOT EXISTS buyer_accounts_customer_id_uq
    ON procure.buyer_accounts (workspace_id, amazon_customer_id)
    WHERE amazon_customer_id IS NOT NULL AND archived_at IS NULL;

-- ── 追加（幂等）：执行入口改造——去掉配对码与「设备绑定账号」，插件按当前登录的买家号 ID 领任务 ────────────
-- 旧库升级，顺序不能变：① 趁旧列还在，把原来的绑定回填成账号的买家号 ID；② devices 增删列；③ buyer_accounts 删
-- default_device_id；④ 删 device_pairings；⑤ execution_runs 加 bj_date / scheduled_slot 与两个唯一索引。
-- 新库上这些语句都是空操作（列已是新形状），索引在这里统一创建。

-- ① 旧绑定回填。对每个未归档、还没登记买家号 ID 的账号：取绑定到它、未撤销的设备里「已登录且 ID 合格」的 ID 集合；
--    集合恰好一个值、这个 ID 没被同工作区其他未归档账号登记、也没有另一个账号在这次回填里要拿同一个 ID → 写进账号，
--    并记 account.customer_id_adopted 事件（via = legacy_binding）。拿不准的一律不动，由所有者在控制台登记。
--    旧列不存在（新库、已升级的库）时整段跳过；引用旧列的语句放在 EXECUTE 里，新库上不会被解析。
DO $$
DECLARE
    r record;
BEGIN
    IF (SELECT count(*) FROM information_schema.columns
        WHERE table_schema = 'procure' AND table_name = 'devices'
          AND column_name IN ('bound_account_id', 'revoked_at')) < 2 THEN
        RETURN;
    END IF;
    FOR r IN EXECUTE $q$
        WITH cand AS (
            SELECT a.id AS account_id, a.workspace_id,
                   upper(btrim(d.identity->>'customer_id')) AS customer_id, min(d.id) AS device_id
            FROM procure.buyer_accounts a
            JOIN procure.devices d ON d.bound_account_id = a.id AND d.workspace_id = a.workspace_id
            WHERE a.archived_at IS NULL AND a.amazon_customer_id IS NULL AND d.revoked_at IS NULL
              AND d.identity->'signed_in' = 'true'::jsonb
              AND upper(btrim(d.identity->>'customer_id')) ~ '^A[0-9A-Z]{8,}$'
            GROUP BY a.id, a.workspace_id, upper(btrim(d.identity->>'customer_id'))
        ), single AS (
            SELECT c.* FROM cand c
            WHERE (SELECT count(*) FROM cand c2 WHERE c2.account_id = c.account_id) = 1
        )
        SELECT s.* FROM single s
        WHERE NOT EXISTS (SELECT 1 FROM procure.buyer_accounts x
                          WHERE x.workspace_id = s.workspace_id AND x.amazon_customer_id = s.customer_id
                            AND x.archived_at IS NULL)
          AND (SELECT count(*) FROM single s2
               WHERE s2.workspace_id = s.workspace_id AND s2.customer_id = s.customer_id) = 1
        ORDER BY s.account_id
    $q$ LOOP
        UPDATE procure.buyer_accounts SET amazon_customer_id = r.customer_id, updated_at = now()
        WHERE id = r.account_id AND workspace_id = r.workspace_id;
        INSERT INTO procure.events (workspace_id, entity, entity_id, actor_kind, kind, detail)
        VALUES (r.workspace_id, 'account', r.account_id, 'system', 'account.customer_id_adopted',
                jsonb_build_object('customer_id', r.customer_id, 'device_id', r.device_id, 'via', 'legacy_binding'));
    END LOOP;
END $$;

-- ② devices：新列 → 用存下的身份回填 → 删旧列。回填只在旧列还在时做一次（之后 login_customer_id 由服务端在每次
--    身份上报时维护，重复执行本脚本不得覆盖它）。已撤销的设备变成「已移除」（hidden_at）；它们的令牌哈希早已清空。
ALTER TABLE procure.devices ADD COLUMN IF NOT EXISTS login_customer_id text;
ALTER TABLE procure.devices ADD COLUMN IF NOT EXISTS hidden_at timestamptz;
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                   WHERE table_schema = 'procure' AND table_name = 'devices' AND column_name = 'revoked_at') THEN
        RETURN;
    END IF;
    EXECUTE $q$
        UPDATE procure.devices SET login_customer_id = upper(btrim(identity->>'customer_id'))
        WHERE login_customer_id IS NULL AND identity->'signed_in' = 'true'::jsonb
          AND upper(btrim(identity->>'customer_id')) ~ '^A[0-9A-Z]{8,}$'
    $q$;
    EXECUTE 'UPDATE procure.devices SET hidden_at = revoked_at WHERE revoked_at IS NOT NULL AND hidden_at IS NULL';
END $$;
ALTER TABLE procure.devices
    DROP COLUMN IF EXISTS bound_account_id,
    DROP COLUMN IF EXISTS identity_status,
    DROP COLUMN IF EXISTS revoked_at;
CREATE INDEX IF NOT EXISTS devices_login_customer_idx
    ON procure.devices (workspace_id, login_customer_id) WHERE login_customer_id IS NOT NULL;

-- ③ buyer_accounts：不再有「定时执行的默认设备」。
ALTER TABLE procure.buyer_accounts DROP CONSTRAINT IF EXISTS buyer_accounts_default_device_fk;
ALTER TABLE procure.buyer_accounts DROP COLUMN IF EXISTS default_device_id;

-- ④ 配对码表整张删除。
DROP TABLE IF EXISTS procure.device_pairings;

-- ⑤ execution_runs：北京日期与定时时间点。旧行的日期按开始时刻回填。
ALTER TABLE procure.execution_runs ADD COLUMN IF NOT EXISTS bj_date date;
ALTER TABLE procure.execution_runs ADD COLUMN IF NOT EXISTS scheduled_slot text
    CHECK (scheduled_slot IS NULL OR scheduled_slot ~ '^([01][0-9]|2[0-3]):[0-5][0-9]$');
UPDATE procure.execution_runs SET bj_date = (started_at AT TIME ZONE 'Asia/Shanghai')::date WHERE bj_date IS NULL;
ALTER TABLE procure.execution_runs ALTER COLUMN bj_date SET NOT NULL;
-- 一台浏览器同时只有一个未结束的批次。旧结构没有这条约束：万一同一台设备留着多个未结束的批次，只留最新的一个，
-- 其余按租约过期结束（它们的在途尝试由下一轮清扫按「所属批次已结束」收尾），否则建索引会失败、整次升级回滚。
UPDATE procure.execution_runs r SET ended_at = now(), end_reason = 'lease_expired'
WHERE r.ended_at IS NULL
  AND EXISTS (SELECT 1 FROM procure.execution_runs n
              WHERE n.device_id = r.device_id AND n.ended_at IS NULL AND n.id > r.id);
CREATE UNIQUE INDEX IF NOT EXISTS execution_runs_one_active_per_device
    ON procure.execution_runs (device_id) WHERE ended_at IS NULL;
-- 一个账号的一个定时时间点一天只执行一次：未结束的、或正常结束（finished）的定时批次占着这个时间点；
-- 租约过期、被清除模拟结果结束的不占，可以再来一次。
CREATE UNIQUE INDEX IF NOT EXISTS execution_runs_slot_once
    ON procure.execution_runs (account_id, bj_date, scheduled_slot)
    WHERE trigger = 'scheduled' AND scheduled_slot IS NOT NULL AND (ended_at IS NULL OR end_reason = 'finished');

-- ── 追加（幂等）：来源指定买家号——任务上存来源指定的两列，分配方式多一种 'source' ──────────────────────
-- 旧库升级：① 加两列；② 把 assignment_kind 的 CHECK 换成三个取值。表上另有一条表级约束也提到 assignment_kind
-- （(assigned_account_id IS NULL) = (assignment_kind IS NULL)，自动名 purchase_orders_check），所以这里只按**确切名字**
-- purchase_orders_assignment_kind_check 处理，并以同名重建（升级后的库与新库的约束名、定义完全一致）；定义里已有
-- 'source' 就什么都不做，重复执行不重建、不重新校验全表。旧任务的指定由 python cli.py db_init 在加列的那一次回填
-- （workflows/db_init：只在这两列第一次加上时做）。
ALTER TABLE procure.purchase_orders ADD COLUMN IF NOT EXISTS designated_customer_id text;
ALTER TABLE procure.purchase_orders ADD COLUMN IF NOT EXISTS designated_buyer_name text;
DO $$
DECLARE
    def text;
BEGIN
    SELECT pg_get_constraintdef(c.oid) INTO def FROM pg_constraint c
    WHERE c.conrelid = 'procure.purchase_orders'::regclass AND c.conname = 'purchase_orders_assignment_kind_check';
    IF def IS NOT NULL AND position('''source''' IN def) > 0 THEN
        RETURN;
    END IF;
    ALTER TABLE procure.purchase_orders DROP CONSTRAINT IF EXISTS purchase_orders_assignment_kind_check;
    ALTER TABLE procure.purchase_orders ADD CONSTRAINT purchase_orders_assignment_kind_check
        CHECK (assignment_kind IN ('auto', 'manual', 'source'));
END $$;
