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
--    ★ 推荐：直接用 watch 大盘的回填页 https://www.jbs-ttj.store/watch/ab/
--      （basic auth 后，每天一行填 浏览/点赞/转发/收藏/评论，自动写回 metrics）
--
--    手工回填（只在页面挂了 / 要批量改的时候用）：
--
--    update public.caption_runs
--       set metrics = '{"views":1200,"likes":53,"shares":8,"collects":21,"comments":7}'::jsonb
--     where board_date = '2026-09-20' and kind = 'daily';
--
--    key 固定这几个：views / likes / shares / collects / comments，
--    另加 "note" 记发布时点等备注。字段可留空，没回填的行 metrics is null，下面会自动跳过。
-- ----------------------------------------------------------------
select
    coalesce(recipe_id, 'builtin')                              as recipe,
    count(*)                                                    as days,
    round(avg((metrics->>'views')::numeric))                    as avg_views,
    round(avg((metrics->>'likes')::numeric), 1)                 as avg_likes,
    round(avg((metrics->>'shares')::numeric), 1)                as avg_shares,
    round(avg((metrics->>'collects')::numeric), 1)              as avg_collects,
    round(avg((metrics->>'comments')::numeric), 1)              as avg_comments,
    -- 互动率 = 真金白银的反馈（赞+藏+评）占曝光多少：文案本身的说服力主要看这个
    round(
        100.0 * (sum((metrics->>'likes')::numeric)
               + sum((metrics->>'collects')::numeric)
               + sum((metrics->>'comments')::numeric))
              / nullif(sum((metrics->>'views')::numeric), 0)
    , 2)                                                        as engage_rate_pct,
    -- 收藏率：清单干货型文案的胜负主要看这个
    round(
        100.0 * sum((metrics->>'collects')::numeric)
              / nullif(sum((metrics->>'views')::numeric), 0)
    , 2)                                                        as collect_rate_pct,
    -- 转发率：转发是唯一能把笔记推出私域的动作，量小但权重高
    round(
        100.0 * sum((metrics->>'shares')::numeric)
              / nullif(sum((metrics->>'views')::numeric), 0)
    , 2)                                                        as share_rate_pct
from public.caption_runs
where kind = 'daily'
  and metrics is not null
  and source = 'llm'          -- 掉回模板兜底的天（LLM 挂了）不参与对比
group by 1
order by engage_rate_pct desc nulls last;

-- ------------------------------------------------------------
-- 4. 异常排查：有没有哪天掉回模板兜底（= 那天 LLM 挂了，数据应剔除）
-- ------------------------------------------------------------
select board_date, coalesce(recipe_id, 'builtin') as recipe, error, caption_len
from public.caption_runs
where kind = 'daily'
  and (source <> 'llm' or error is not null)
order by board_date desc;
