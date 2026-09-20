-- ============================================================
-- 小红书文案配方（A/B 实验）· Supabase 表结构
-- 在 Supabase Dashboard -> SQL Editor 里整段执行一次即可（可重复执行）
--
-- 为什么必须手工建表：PostgREST（REST 接口）只能做 DML，不能执行 DDL，
-- service_role key 也没有这个权限 —— 这是 Supabase 的限制，不是配置问题。
--
-- 用法：在 caption_recipes 里配好若干配方（enabled=true），服务每天出榜时
-- 按 board_date 稳定哈希挑一套生成文案，并把「当天用了哪套」写进 caption_runs。
-- 表不存在 / 查不到 / 全 disabled 时自动回退到代码内置配方，不会出空文案。
-- ============================================================

-- ------------------------------------------------------------
-- 1. 文案配方：一套配方 = 一条 prompt + 一组采样参数
-- ------------------------------------------------------------
create table if not exists public.caption_recipes (
    id                 text primary key,          -- 'a_hook' 之类稳定键，改了等于换配方
    name               text not null,             -- 展示名，会显示在素材页上
    enabled            boolean not null default true,
    weight             int not null default 1,    -- 轮换权重，等权就都填 1

    system_prompt      text,                      -- null = 用内置 system
    user_prompt        text,                      -- null = 用内置模板；支持占位符见下

    model              text,                      -- null = 用 HS_LLM_MODEL
    temperature        numeric(3, 2),             -- null = 用内置 0.8
    top_p              numeric(3, 2),             -- null = 不传
    max_tokens         int,                       -- null = 用内置 400
    repetition_penalty numeric(3, 2),             -- null = 用内置 1.15

    max_len            int,                       -- 清洗截断字数，null = 600
    include_parsed     boolean not null default true,  -- 是否把「已解析未入榜」本喂进去
    parsed_ratio       numeric(3, 2),             -- 已解析本筛选：热度 >= 榜首 × ratio
    parsed_max         int,                       -- 最多提几本

    note               text,                      -- 给自己看的备注（这个 variant 想验证什么）
    updated_at         timestamptz not null default now(),

    constraint ck_recipe_weight check (weight > 0)
);

comment on table  public.caption_recipes is '小红书文案 A/B 配方：每天按日期哈希轮换一套';
comment on column public.caption_recipes.user_prompt is
    '占位符：{date} {top_n} {top1} {summary} {parsed_hint} {parsed_names}';
comment on column public.caption_recipes.note is '这个 variant 想验证什么假设，事后回看时靠它回忆';

-- ------------------------------------------------------------
-- 2. 文案留痕：哪天用了哪套、出了什么，便于几周后回看对比
-- ------------------------------------------------------------
create table if not exists public.caption_runs (
    id           uuid primary key default gen_random_uuid(),
    board_date   date not null,
    kind         text not null default 'daily',   -- daily / weekly（当前只有 daily）

    recipe_id    text,                            -- null = 走了内置兜底配方
    recipe_name  text,
    source       text not null,                   -- llm / template
    model        text,
    temperature  numeric(3, 2),

    caption      text,                            -- 全文，事后不用再去翻文件
    caption_len  int,
    error        text,

    metrics      jsonb,                           -- 预留：手动回填互动数据
                                                  -- 例 {"views":1200,"likes":53,"collects":21,"note":"9/20 发布"}
    created_at   timestamptz not null default now(),

    constraint uq_caption_run unique (board_date, kind)
);

create index if not exists idx_caption_runs_date on public.caption_runs (board_date desc);

comment on table  public.caption_runs is '每天文案用了哪套配方 + 产出，A/B 实验归因用';
comment on column public.caption_runs.metrics is '预留给手动回填的小红书互动数据（浏览/赞/藏）';

-- ------------------------------------------------------------
-- 3. RLS：配方和留痕都是内部数据，只走 service_role
-- ------------------------------------------------------------
alter table public.caption_recipes enable row level security;
alter table public.caption_runs   enable row level security;

drop policy if exists "caption_recipes service only" on public.caption_recipes;
create policy "caption_recipes service only"
    on public.caption_recipes for select
    to service_role
    using (true);

drop policy if exists "caption_runs service only" on public.caption_runs;
create policy "caption_runs service only"
    on public.caption_runs for select
    to service_role
    using (true);

-- ------------------------------------------------------------
-- 4. 开箱两套配方：a_hook = 现状（钩子型），b_list = 清单干货型
--    想加 variant 就照着 insert 一条，改 enabled/weight 即可调流量
-- ------------------------------------------------------------
insert into public.caption_recipes
    (id, name, enabled, weight, system_prompt, user_prompt, note)
values
    ('a_hook', '钩子型（现状）', true, 1,
     '你是小红书剧本杀垂类博主，文案口语化、有情绪、有钩子。',
     E'以下是今天（{date}）的杭州剧本杀热度榜 Top{top_n}：\n'
     E'{summary}\n\n'
     E'请以小红书剧本杀垂类博主的语气写一段发布文案，要求：\n'
     E'1. 第一行是标题，带 1-2 个 emoji，要有钩子（点出榜首或最大黑马）\n'
     E'2. 正文 2-4 句话，口语化，点出 1-3 个值得关注的点（榜首、上升快、新上榜）\n'
     E'3. 结尾 3-5 个话题标签，如 #剧本杀 #杭州剧本杀 #周末去哪儿\n'
     E'{parsed_hint}'
     E'直接输出文案，不要任何解释或前后缀。',
     '基线组：验证「钩子标题 + 口语短句」的原生表现'),
    ('b_list', '清单干货型', true, 1,
     '你是小红书剧本杀垂类博主，擅长把榜单讲成一份可直接抄作业的清单。',
     E'以下是今天（{date}）的杭州剧本杀热度榜 Top{top_n}：\n'
     E'{summary}\n\n'
     E'请写一段「可以直接抄作业」的小红书清单文案，要求：\n'
     E'1. 第一行是标题，点明日期和城市，让人一眼知道这是今天的杭州榜单\n'
     E'2. 正文按名次列 Top3，每本一句话说清「适合谁 / 为什么值得去」，不要只报排名\n'
     E'3. 数据只能用上面给的，禁止编造热度值、名次或剧本名\n'
     E'4. 结尾一句引导收藏（比如「周末要打的先码住」）\n'
     E'5. 末尾 4-5 个话题标签\n'
     E'{parsed_hint}'
     E'直接输出文案，不要任何解释或前后缀。',
     '实验组：验证「清单化 + 每本给理由」是否比钩子型更容易被收藏')
on conflict (id) do nothing;
