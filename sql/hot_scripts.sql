-- ============================================================
-- 剧本杀每日热门榜 Top10 · Supabase 表结构
-- 在 Supabase Dashboard -> SQL Editor 里整段执行一次即可（可重复执行）
--
-- 为什么必须手工建表：PostgREST（REST 接口）只能做 DML，不能执行 DDL，
-- service_role key 也没有这个权限 —— 这是 Supabase 的限制，不是配置问题。
-- 服务启动自检（hotsearch doctor）会检查表是否存在并在缺失时提示执行本文件。
-- ============================================================

-- ------------------------------------------------------------
-- 1. 每日榜单明细：一天 10 行
-- ------------------------------------------------------------
create table if not exists public.script_hot_daily (
    id            uuid primary key default gen_random_uuid(),

    board_date    date not null,                -- 榜单日期（Asia/Shanghai）
    rank          smallint not null,            -- 1..10
    title         text not null,                -- 展示标题
    title_key     text not null,                -- 归一化去重键

    hot_score     numeric(6, 2) not null,       -- 综合热度 0~100
    prev_rank     smallint,                     -- 上一期名次；null = 新上榜
    is_new        boolean not null default false,

    tags          text[],                       -- ['推理','硬核'] 之类
    players       text,                         -- '6人（3男3女）'
    duration      text,                         -- '6小时'
    rating        numeric(3, 1),                -- 平台评分（0~10）
    cover_url     text,
    reason        text,                         -- 一句话上榜理由

    sources       text[],                       -- ['miquan','search_llm']
    source_detail jsonb,                        -- 各源原始信号，便于回溯「凭什么这么排」
    created_at    timestamptz not null default now(),

    constraint uq_hot_daily_date_rank  unique (board_date, rank),
    constraint uq_hot_daily_date_title unique (board_date, title_key),
    constraint ck_hot_daily_rank check (rank between 1 and 50)
);

create index if not exists idx_hot_daily_date on public.script_hot_daily (board_date desc);

comment on table  public.script_hot_daily is '每日剧本杀热门榜 Top10（jbs-hotsearch 服务产出）';
comment on column public.script_hot_daily.source_detail is '{源: {value, weight, heat, score}}，融合打分的原始依据';

-- ------------------------------------------------------------
-- 2. 运行留痕：每天一次谁跑过、跑到没
-- ------------------------------------------------------------
create table if not exists public.script_hot_runs (
    id            uuid primary key default gen_random_uuid(),
    board_date    date not null,
    started_at    timestamptz not null default now(),
    finished_at   timestamptz,
    status        text not null,                -- success / partial / failed
    duration_ms   int,
    item_count    int,
    source_status jsonb,                        -- [{source, ok, items, error}]
    store         text,                         -- supabase / local
    error         text
);

create index if not exists idx_hot_runs_date on public.script_hot_runs (board_date desc);

-- ------------------------------------------------------------
-- 3. 最新一期榜单视图：H5 / 后端直接读这个，不用自己 order+limit
-- ------------------------------------------------------------
create or replace view public.script_hot_latest as
select d.*
from public.script_hot_daily d
where d.board_date = (select max(board_date) from public.script_hot_daily);

-- ------------------------------------------------------------
-- 4. RLS：榜单是可公开读的只读数据
-- ------------------------------------------------------------
alter table public.script_hot_daily enable row level security;
alter table public.script_hot_runs  enable row level security;

drop policy if exists "hot_daily readable by anon" on public.script_hot_daily;
create policy "hot_daily readable by anon"
    on public.script_hot_daily for select
    to anon, authenticated
    using (true);

-- 运行日志不对前端暴露（读写都只走 service_role）
drop policy if exists "hot_runs service only" on public.script_hot_runs;
create policy "hot_runs service only"
    on public.script_hot_runs for select
    to service_role
    using (true);
