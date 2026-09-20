-- ============================================================
-- 文案 A/B 实验回看：跑 3~6 周后在 Supabase SQL Editor 执行
-- 只读查询，不会改动任何数据
-- ============================================================

-- ------------------------------------------------------------
-- 1. 各配方各跑了几天（先看流量分得均不均，不均的结论不可信）
-- ------------------------------------------------------------
select
    coalesce(r.recipe_id, 'builtin')              as recipe,
    count(*)                                      as days,
    min(r.board_date)                             as first_day,
    max(r.board_date)                             as last_day,
    round(avg(r.caption_len))                     as avg_len
from public.caption_runs r
where r.kind = 'daily'
group by 1
order by days desc;

-- ------------------------------------------------------------
-- 2. 逐天明细：哪天用了哪套、出了多长的文案
-- ------------------------------------------------------------
select
    board_date,
    coalesce(recipe_id, 'builtin')  as recipe,
    source,
    model,
    caption_len,
    left(caption, 60)               as caption_head
from public.caption_runs
where kind = 'daily'
order by board_date desc
limit 60;

-- ------------------------------------------------------------
-- 3. 效果对比（需要先把互动数据回填进 metrics）
--
--    回填示例（在小红书发了笔记、拿到数据后）：
--
--    update public.caption_runs
--       set metrics = '{"views":1200,"likes":53,"collects":21,"comments":7}'::jsonb
--     where board_date = '2026-09-20' and kind = 'daily';
--
--    字段随意扩展，比如加 "note":"9/20 晚上 8 点发的"。
--    没回填的行 metrics is null，下面的查询会自动跳过。
-- ------------------------------------------------------------
select
    coalesce(recipe_id, 'builtin')                              as recipe,
    count(*)                                                    as days,
    round(avg((metrics->>'views')::numeric))                    as avg_views,
    round(avg((metrics->>'likes')::numeric), 1)                 as avg_likes,
    round(avg((metrics->>'collects')::numeric), 1)              as avg_collects,
    round(avg((metrics->>'comments')::numeric), 1)              as avg_comments,
    -- 收藏率：清单型文案的胜负主要看这个
    round(
        100.0 * sum((metrics->>'collects')::numeric)
              / nullif(sum((metrics->>'views')::numeric), 0)
    , 2)                                                        as collect_rate_pct
from public.caption_runs
where kind = 'daily'
  and metrics is not null
group by 1
order by collect_rate_pct desc nulls last;

-- ------------------------------------------------------------
-- 4. 异常排查：有没有哪天掉回模板兜底（= 那天 LLM 挂了，数据应剔除）
-- ------------------------------------------------------------
select board_date, coalesce(recipe_id, 'builtin') as recipe, error, caption_len
from public.caption_runs
where kind = 'daily'
  and (source <> 'llm' or error is not null)
order by board_date desc;
