-- 采购控制台建表脚本（幂等，db_init 执行）。契约见 docs/development/开发契约-v2.md。
-- PostgreSQL 16 / 17。所有时间为 timestamptz（UTC）；业务日期为北京日期，由应用层计算后写入 date 列。

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

-- ── 买家账号与设备 ────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS procure.buyer_accounts (
    id                   bigserial PRIMARY KEY,
    workspace_id         bigint NOT NULL REFERENCES procure.workspaces(id),
    name                 text NOT NULL,                       -- 与飞书「买家号」取值一致，如 环境97招商
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
    default_device_id    bigint,                              -- 定时执行的默认设备（FK 见下）
    expected_card_last4  text CHECK (expected_card_last4 IS NULL OR expected_card_last4 ~ '^[0-9]{4}$'),
    notes                text,
    created_at           timestamptz NOT NULL DEFAULT now(),
    updated_at           timestamptz NOT NULL DEFAULT now(),
    archived_at          timestamptz,
    UNIQUE (workspace_id, name)
);

CREATE TABLE IF NOT EXISTS procure.devices (
    id                bigserial PRIMARY KEY,
    workspace_id      bigint NOT NULL REFERENCES procure.workspaces(id),
    name              text NOT NULL,
    browser           text,                                   -- chrome / adspower / other（插件上报）
    token_hash        text UNIQUE,                            -- sha256(device_token) hex；撤销后置 NULL
    bound_account_id  bigint REFERENCES procure.buyer_accounts(id),
    ext_version       text,
    user_agent        text,
    last_seen_at      timestamptz,
    identity          jsonb,                                  -- {signed_in, customer_id, site, page_url, at}
    identity_status   text NOT NULL DEFAULT 'unknown' CHECK (identity_status IN ('unknown', 'match', 'mismatch', 'signed_out')),
    identity_at       timestamptz,
    created_at        timestamptz NOT NULL DEFAULT now(),
    revoked_at        timestamptz
);

DO $$ BEGIN
    ALTER TABLE procure.buyer_accounts
        ADD CONSTRAINT buyer_accounts_default_device_fk
        FOREIGN KEY (default_device_id) REFERENCES procure.devices(id);
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

CREATE TABLE IF NOT EXISTS procure.device_pairings (
    code_hash     text PRIMARY KEY,                           -- sha256(code) hex
    workspace_id  bigint NOT NULL REFERENCES procure.workspaces(id),
    created_by    bigint REFERENCES procure.users(id),
    created_at    timestamptz NOT NULL DEFAULT now(),
    expires_at    timestamptz NOT NULL,
    used_at       timestamptz,
    device_id     bigint REFERENCES procure.devices(id)
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
    conflict                 jsonb,                           -- {source_kind, fields: {字段: [当前, 来源]}, at}
    needs_attention          boolean NOT NULL DEFAULT false,
    attention_note           text,
    primary_source_kind      text NOT NULL CHECK (primary_source_kind IN ('feishu', 'file')),
    assigned_account_id      bigint REFERENCES procure.buyer_accounts(id),
    assignment_kind          text CHECK (assignment_kind IN ('auto', 'manual')),
    assigned_at              timestamptz,
    assignment_note          text,
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
    end_reason        text,                                   -- finished / lease_expired / account_paused / device_revoked
    stats             jsonb NOT NULL DEFAULT '{}'
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
    stats         jsonb NOT NULL,                             -- {considered, assigned, coverage, fill, unassigned}
    uncovered     jsonb NOT NULL DEFAULT '[]',                -- [{account_id, name, date, reason}]
    unassigned    jsonb NOT NULL DEFAULT '[]'                 -- [{order_id, upstream_order_no, reason}]
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
