# jbs-hotsearch · 剧本杀每日热门榜 Top10

每天跑一次的常驻服务：去网上抓当前最热门的 10 个剧本杀，融合打分后写进 Supabase，顺手出一份 Markdown 报告。
前端可以直接读 Supabase 的 `script_hot_latest` 视图拿到「今天最热的 10 个本」。

```
          ┌──────────── 数据源（可插拔）────────────┐
          │  miquan（米圈签名 API，硬数据，默认开）  │
          │  search_llm（搜索 + LLM 聚合，可选）     │
          └───────────────┬────────────────────────┘
                          │  ScriptCandidate（各自把平台信号压成 0~1）
                          ▼
                  normalize + rank_fuse（跨源去重 / 加权 / 多源加成）
                          ▼
              prev_ranks ←─┐  排名涨跌
                           │  ▼
              Store  ──► Supabase script_hot_daily（主）
                     └─► 本地 SQLite + JSON 快照（降级）
                          ▼
                  Markdown 报告 + 运行日志
```

---

## 1. 五分钟跑起来

```bash
python -m venv .venv && source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env                                   # 填 Supabase 凭据

python -m jbs_hotsearch preview      # 只算不写，先看看榜单长什么样
python -m jbs_hotsearch doctor       # 环境与权限自检
python -m jbs_hotsearch once         # 正式跑一次：写库 + 写报告
```

`once` 的产物：

- Supabase：`script_hot_daily`（10 行）+ `script_hot_runs`（1 行运行日志）
- 本地：`data/reports/YYYY-MM-DD.md`（人看的报告）、Supabase 不可用时额外在 `data/snapshots/` 留 JSON 兜底

## 2. Supabase 一次性建表（必做）

PostgREST 不能执行 DDL，service_role key 也不行 —— 这一步必须手工做一次：

> Supabase Dashboard → SQL Editor → 粘贴 `sql/hot_scripts.sql` 全文 → Run

建出来的东西：

| 对象 | 用途 |
|---|---|
| `script_hot_daily` | 每日 10 行榜单，含 `hot_score` / `prev_rank` / `is_new` / `source_detail` |
| `script_hot_runs` | 每次运行的留痕（状态、耗时、各源成败），排查「今天怎么没出榜」用 |
| `script_hot_latest` | 最新一期榜单视图，前端只读它即可 |
| RLS 策略 | 榜单对 anon 公开读；运行日志仅 service_role 可见 |

没建表也能跑：`HS_STORE_BACKEND=auto` 会在日志里明确报错并降级到本地 SQLite，
不至于「任务静默失败、第二天才发现没数据」。但既然要用，建议还是花一分钟建了。

## 3. 数据源

### 3.1 米圈（`miquan`，默认开）

接口 `juzujujk.joylovemeet.cn/v9/script/scriptSearchPage` 每个请求都带 `sign`，而 **sign 覆盖整个请求体**：改一个字段（连 `pageNum` 都不行）就 `400003`。
所以这层的做法是**原样回放抓到的 curl**，一行一个页 —— 好消息是 sign 不绑时间（8 月抓的包 9 月照样通），一次抓包能长期复用。

- 输入：`HS_SOURCE_MIQUAN_CURLS_FILE`（默认 `data/miquan_curls.txt`，仓库里已放了 26 页 ≈ 519 本）
- 扩容：在米圈 App 翻页，Charles 导出 curl，一行一条追加进这个文件，代码不用动
- 热度口径：`recommendNum`（平台热度 0~100，每次请求实时刷新）+ `scriptScore`（评分 0~10）

### 3.2 搜索 + LLM 聚合（`search_llm`，可选）

米圈只能看到「平台自己的热度」，这个源补上外部讨论热度（评测文章、小红书/公众号榜单、门店开本榜）：
多组查询 → 搜索 API → LLM 抽结构化 → 按「被多少处结果提到」算值。

```env
HS_SEARCH_PROVIDER=bocha          # bocha / tavily / serpapi
HS_SEARCH_API_KEY=sk-xxx
HS_LLM_BASE_URL=https://api.siliconflow.cn/v1
HS_LLM_API_KEY=sk-xxx
HS_LLM_MODEL=Qwen/Qwen2.5-72B-Instruct
```

没配就 `ok=False` 显式降级（**绝不返回写死的榜单**），报告顶部的「降级说明」会写清楚哪条源没起来。

## 4. 热度怎么算的

每个源只做一件事：把自家信号压成 0~1 的 `value`。跨源可比性放在 `rank.py`：

```
base   = Σ(weight_i × value_i) / Σ(weight_i)         # 加权和
boost  = base × (1 + 0.15 × (source_count - 1))      # 多源交叉验证加成
score  = min(1, boost + 新鲜度加成) × 100            # 新鲜度：近 120 天新本最多 +10
```

米圈的 `value` 是**绝对值**（`0.75 × 热度/100 + 0.25 × 评分/10`），不用「除以当天最大值」归一化 ——
否则今天的分数会被当天的极值绑架，跨天没法比。代价是分数集中在 85~92，这是真实分布，不是 bug。

## 5. 部署：每天自动跑

服务自带调度器（`scheduler.py`，纯标准库），到点执行一次，单进程天然互斥。Linux 服务器挑 A 或 B，本机长期开着选 C。

### A. Docker on 服务器（推荐：环境可复现、更新一条命令）

把代码弄到服务器上：

```bash
# 方式一：仓库已有 remote，直接 clone（记得先 push）
git clone git@github.com:zhuishaolost2fa/jbs-hotsearch.git /opt/jbs-hotsearch
# 方式二：本机 rsync / scp 上去
rsync -av --exclude '.venv' --exclude '__pycache__' ./ user@server:/opt/jbs-hotsearch/
```

在服务器上：

```bash
cd /opt/jbs-hotsearch
cp .env.example .env          # 填 SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY
chmod +x deploy/server-deploy.sh
sudo ./deploy/server-deploy.sh docker up        # 构建镜像 + 启动
sudo ./deploy/server-deploy.sh docker doctor    # 自检：源能不能拉、Supabase 通不通
sudo ./deploy/server-deploy.sh docker once      # 立刻出一次榜，确认端到端跑通
```

日常运维：

```bash
sudo ./deploy/server-deploy.sh docker logs      # 跟日志
sudo ./deploy/server-deploy.sh docker status    # 容器状态 + 健康检查位
sudo ./deploy/server-deploy.sh docker update    # 代码更新后重建镜像并滚动重启
```

容器里代码是 build 进去的（不是挂载），所以**改完代码必须 `update` 重建镜像**才生效；`.env` 和 `data/` 是挂载的，改它们重启即可。

> 注意：`TZ=Asia/Shanghai` 已经在 `docker-compose.yml` 里写死。别删 —— 容器默认 UTC，`HS_RUN_AT=09:00` 会变成北京时间 17 点出榜。

### B. 裸机 systemd（不想上 Docker 就用这个）

```bash
cd /opt/jbs-hotsearch
cp .env.example .env          # 同上填好
sudo ./deploy/server-deploy.sh systemd install  # venv + 装包 + 注册开机自启
sudo ./deploy/server-deploy.sh systemd once
journalctl -u jbs-hotsearch -f                  # 或 systemd logs
```

### C. Windows 计划任务（本机长期开着）

```powershell
schtasks /Create /TN jbs-hotsearch /SC ONLOGON /DELAY 0001:00 ^
  /TR "cmd /c \"C:\path\to\jbs-hotsearch\.venv\Scripts\hotsearch.exe\" serve" /F
```

进程每天只醒一次（其余时间在 sleep），内存几十 MB，可以忽略。

### 上线后怎么确认它真的在跑

```bash
docker ps --filter name=jbs-hotsearch        # STATUS 里 unhealthy 说明 48h 没出过报告
```

最终判据还是 Supabase —— 每天应该多一行 `status=success`：

```sql
select board_date, status, item_count, duration_ms, source_status
from script_hot_runs order by board_date desc limit 7;
```

### 环境变量速查

| 变量 | 默认 | 说明 |
|---|---|---|
| `HS_RUN_AT` | `09:00` | 每天几点跑 |
| `HS_TIMEZONE` | `Asia/Shanghai` | 时区（Windows 下靠 `tzdata` 包解析） |
| `HS_TZ_FALLBACK_OFFSET` | `8` | 时区解析失败时的固定偏移（小时），避免服务起不来 |
| `HS_RUN_ON_START` | `true` | 启动时是否补跑当天 |
| `HS_TOP_N` | `10` | 榜单条数 |
| `HS_STORE_BACKEND` | `auto` | `auto` / `supabase` / `local` |
| `HS_SOURCE_MIQUAN_ENABLED` | `true` | 米圈源开关 |
| `HS_SOURCE_MIQUAN_CURLS_FILE` | `./data/miquan_curls.txt` | 抓包 curl 文件 |
| `HS_SEARCH_PROVIDER` | `none` | `none` / `bocha` / `tavily` / `serpapi` |
| `HS_CROSS_SOURCE_BOOST` | `0.15` | 多源交叉验证加成 |
| `HS_RECENCY_BOOST` | `0.10` | 新本新鲜度加成 |

## 6. 排障

| 症状 | 原因与处理 |
|---|---|
| `PGRST205 ... does not exist` | 表没建，去执行 `sql/hot_scripts.sql` |
| `page 3 head={'code': 400003}` | sign 对不上，重新抓那一页 curl 覆盖进 `miquan_curls.txt` |
| 报告里全是「🆕 新上榜」 | 上一期没数据（首次运行或昨天没跑成功），属正常 |
| 榜单条目数为 0 | 全源失败，`once` 会直接报错退出，**不会**写旧数据充数 |

看历史榜单：`python -m jbs_hotsearch board`（本地快照）；Supabase 里直接查视图：

```bash
curl "$SUPABASE_URL/rest/v1/script_hot_latest?select=rank,title,hot_score,prev_rank" \
  -H "apikey: $SUPABASE_ANON_KEY"
```
