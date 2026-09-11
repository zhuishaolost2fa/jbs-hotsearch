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
                          ▼
              监听大盘（热门榜 / DM 解析 / SEO·GEO 是否跑成）
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
python -m jbs_hotsearch status       # 看最近这些天跑没跑成（详见第 6 节）
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

> `install` 会注册**两个** unit：`jbs-hotsearch`（出榜）和 `jbs-hotsearch-watch`（监听大盘）。
> 分开是为了出榜挂掉时大盘还活着。watch 默认只听 `127.0.0.1:8787`，要远程访问改 unit 里的 `HS_WATCH_HOST`。

### C. Windows 计划任务（本机长期开着）

```powershell
schtasks /Create /TN jbs-hotsearch /SC ONLOGON /DELAY 0001:00 ^
  /TR "cmd /c \"C:\path\to\jbs-hotsearch\.venv\Scripts\hotsearch.exe\" serve" /F
```

进程每天只醒一次（其余时间在 sleep），内存几十 MB，可以忽略。

### D. CI/CD：push 到 main 自动部署（可选）

仓库自带两个工作流，配好后就是「本地 push → 服务器自动重建」：

| 工作流 | 触发时机 | 干什么 |
|---|---|---|
| `.github/workflows/ci.yml` | push / PR 到 main | `ruff` 静态检查 → 全量编译 → `status` 冒烟（Python 3.11 / 3.12 各跑一遍） |
| `.github/workflows/deploy.yml` | CI 跑**绿**之后，或手动 Run workflow | SSH 到服务器 `git pull` → `server-deploy.sh docker update` → 打 `/healthz` 探活 |

**一次性配置**（Settings → Secrets and variables → Actions）：

| 位置 | 名称 | 说明 |
|---|---|---|
| Secrets | `SSH_PRIVATE_KEY` | **必填**。能免密登录服务器的私钥，ed25519 整段含首尾行（`-----BEGIN` / `-----END` 也要） |
| Secrets | `SSH_HOST` | **必填**。服务器域名或 IP |
| Secrets | `SSH_USER` | **必填**。登录用户名 —— 别信「默认 root」，绝大多数云主机是 `ubuntu` / `ec2-user`，填错只会在最后一步 `Permission denied` |
| Secrets | `SSH_PORT` | 可选，默认 `22` |
| Variables | `APP_DIR` | 可选，默认 `/opt/jbs-hotsearch` |

这 3 个必填项缺任何一个，**部署工作流都会红，而且每次 CI 一转绿就自动红一次**
（它是 `workflow_run` 触发的，不是偶发）。报错长这样：

```
::error::缺 Secret：SSH_USER（本项目填 ubuntu）
::error::去 Settings → Secrets and variables → Actions 配上再跑
```

私钥建议专门生成一把、只给它部署权限，别拿你自己登录用的那把：

```bash
ssh-keygen -t ed25519 -f ~/.ssh/gha_deploy -N "" -C "github-actions-deploy@<项目>"
ssh-copy-id -i ~/.ssh/gha_deploy.pub <用户>@<服务器>     # 或手动把 .pub 追加进服务器 ~/.ssh/authorized_keys
cat ~/.ssh/gha_deploy                                    # 整段粘到 SSH_PRIVATE_KEY
```

本机想确认这把钥匙行不行，**一定要按 runner 的方式验**（交互式能连不代表批处理能连）：

```bash
ssh -i ~/.ssh/gha_deploy -o IdentitiesOnly=yes -o BatchMode=yes <用户>@<服务器> \
    'cd <APP_DIR> && git pull --ff-only && docker compose version'
```

还有一条容易漏：**服务器自己要能免密 `git pull`**。GitHub Actions 只是 SSH 上去执行 `git pull`，服务器拉代码用的是它自己的身份：

- 服务器 origin 是 HTTPS 且仓库公开 → 直接就能拉，什么都不用配
- 服务器 origin 是 `git@github.com:...` 或仓库私有 → 配一个只读 Deploy Key（Settings → Deploy keys），或改 HTTPS + token

还有个更常见的坑：**服务器工作区必须是干净的**。`git pull --ff-only` 遇到本地未提交改动会直接失败，
CI 上看到的就是 `error: Your local changes would be overwritten`。
所以服务器上别直接改代码 —— 改了就提交推回 GitHub，别留在工作区里。

PR 只跑检查不部署；只有 push 到 main 且 CI 全绿才会上线。手动触发在 Actions → Deploy → Run workflow。

> CI 里**故意不装** `httpx` / `playwright`（后者还要拉 chromium，又慢又重）。
> `status` / `watch` 走的是延迟导入，零第三方依赖就能跑 —— 所以冒烟验的是「代码没写崩、无 `.env` 时降级路径不炸」，不验真实数据源。数据源那部分归服务器上的 `docker doctor` 和 `docker once` 管。

本地想跑跟 CI 完全一样的检查：

```bash
pip install ruff
ruff check src/                                  # 规则写在 pyproject.toml 的 [tool.ruff.lint]
python -m compileall -q src/
PYTHONPATH=src python -m jbs_hotsearch status --days 1
```

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
| `HS_WATCH_HOST` / `HS_WATCH_PORT` | `127.0.0.1` / `8787` | 大盘监听地址（容器里要 `0.0.0.0`） |
| `HS_WATCH_DAYS` | `30` | 大盘回看窗口 |
| `HS_WATCH_GRACE_MINUTES` | `60` | 超时多久判「今天没跑」 |
| `HS_WATCH_REFRESH` | `60` | 页面自动刷新秒数 |
| `HS_WATCH_CACHE_SECONDS` | `20` | 服务端回源最小间隔 |
| `HS_WATCH_WEBHOOK_URL` | 空 | 告警出口，留空不告警 |
| `HS_WATCH_SITE_ORIGIN` | `https://www.jbs-ttj.store` | SEO / GEO 产物探测的线上域名 |
| `HS_WATCH_DM_ENABLED` | `true` | 是否盯 DM 手册解析 |
| `HS_WATCH_SEO_ENABLED` | `true` | 是否盯 SEO / GEO 产物 |
| `HS_WATCH_STUCK_HOURS` | `2` | 中间态任务卡多久算僵尸 |
| `HS_WATCH_HTTP_TIMEOUT` | `6` | 探测单个线上产物的超时（并发探测） |

## 6. 监听大盘：每天到底跑没跑成

「今天怎么没出榜」这类问题最怕的是**静默失败** —— 进程活着、日志没人看、榜单停更三天才发现。
所以项目自带一个只读的监听大盘，盯三件事，回答同一个问题：**今天，到底跑成没跑成**。

| 任务 | 看板 | 留痕在哪 | 性质 |
|---|---|---|---|
| **每日热门榜** | `hotsearch` | Supabase `script_hot_runs` | 每天必须跑，没跑 = 缺跑 |
| **DM 手册解析** | `dm_ingest` | Supabase `script_dm_jobs`（jbsttj-backend 共用同一个 Supabase） | 上传手册才触发，**没任务是常态** |
| **SEO / GEO 产物** | `seo_geo` | 线上静态文件 `sitemap.xml` / `llms.txt` / `feed.xml`… | 每次部署构建，看产物在不在、对不对 |

三者的判定规则刻意不同 —— 用同一套「没跑就是挂了」去套，DM 解析会天天误报（它本来就不是每天有活），
SEO 又会在网络抖动时误报（探测不到 ≠ 生成失败）。

```bash
python -m jbs_hotsearch status              # 终端里直接看（适合 SSH / crontab 邮件）
python -m jbs_hotsearch status --days 7 --json
python -m jbs_hotsearch watch               # 起 Web 大盘，默认 http://127.0.0.1:8787
python -m jbs_hotsearch watch --emit data/dashboard/index.html   # 只生成一份静态页，不启服务
```

`status` 按任务分段打印（`✖` 失败 / `·` 缺跑 / `!` 可疑 / `✔` 成功 / `–` 无任务 / `…` 未到点 / `⏱` 卡住）：

```
── 每日热门榜（每天）
日期          结论         任务    产出       耗时  说明
2026-09-09  ✔ 成功      23    10     3.3s  miquan miquan_group
2026-09-08  · 缺跑        -     -        -
   成功率 3% (1/29) · 连续 1 天

── DM 手册解析（按需）
2026-09-09  – 无任务       -     -        -
2026-09-08  ✖ 失败       19    17        -  17 完成 / 2 失败 · 1542 块 / 4799 问答
             └─ 2 个失败，首个：mao-dao-mou-sha-xun-huan ChordError: ...DatabaseError

── SEO / GEO 产物（每次部署）
   ✔ /robots.txt      成功  http=200   44 字节
   ✖ /llms.txt        失败  http=404   产物不存在（404）—— 这次构建可能没生成它
```

有告警时 `status` 退出码为 1，可直接接进 crontab / CI。
Web 大盘则是：顶部三张任务卡（今日状态 + 关键指标），下面每块任务一个日历热力图 + 逐日明细，
SEO / GEO 那块是产物清单（可点击直接打开）。页面每 60s 自动刷新。

### 公网地址 / 怎么看

`watch` 默认只监听 `127.0.0.1:8787`，服务器上的 8787 在云安全组里没放行（也**不建议**放行 ——
页面里有 Supabase 报错原文、内部 URL、剧本名）。三种看法：

| 方式 | 命令 / 地址 | 适用 |
|---|---|---|
| 本机 | http://127.0.0.1:8787 | 在服务器上直接看 |
| SSH 隧道 | `ssh -N -L 8787:127.0.0.1:8787 <服务器>` 然后开 http://127.0.0.1:8787 | 临时看一眼，最安全 |
| **nginx 反代（已配）** | `http://<服务器IP或域名>/watch/` | 长期用，走现成的 80 端口，带 basic auth |

反代配置见 `deploy/nginx-watch.conf`，直接把里面的 `location` 块插进 `/opt/jbs/deploy/nginx.conf`
的 `server { }` 里，**位置必须在 `location / {...}`（SPA fallback）之前**。
两个坑写在那个文件的注释里了：口令文件是 `docker cp` 进容器的（容器重建会丢，宿主机那份才持久）、
`proxy_pass` 末位的 `/` 不能少（否则前缀剥不掉，watch 收到 `/watch/` 会 404）。

大盘页面是自包含单文件 HTML，没有外部资源、没有绝对路径引用，刷新用的也是相对 URL，
所以挂在 `/watch/` 这种子路径下不需要改任何代码。

### 判定规则

通用等级：`✔ 成功` / `! 可疑` / `✖ 失败` / `· 缺跑` / `… 未到点` / `– 无任务` / `▶ 进行中` / `⏱ 卡住` / `? 未知`。

热门榜（每天型）：

| 结论 | 判据 | 颜色 |
|---|---|---|
| 成功 | `status=success` 且真的写出了条目 | 绿 |
| 可疑 | 跑完了但**条目数为 0**，或部分源失败（`partial`） | 黄 |
| 失败 | `status=failed` | 红 |
| 缺跑 | 这天**根本没有运行记录** | 灰 |
| 未到点 | 今天还没到 `HS_RUN_AT` + 宽限期（不算异常） | 白框 |

DM 手册解析（按需型）：`失败` 优先于 `进行中` 优先于 `成功`；**当天没有任务记为「无任务」，不是异常**。
额外抓一件热门榜没有的事 —— **僵尸任务**：状态停在 `pending/downloading/extracting/chunking/generating_qa/embedding`
超过 `HS_WATCH_STUCK_HOURS`（默认 2 小时）的任务会单独告警，这基本等于「Celery worker 没在消费」。

SEO / GEO（产物型）：对每个产物发一次 HTTP 请求，按「状态码 + 内容」判定：

| 情况 | 结论 |
|---|---|
| 200 且内容含预期标记（`sitemap.xml` 有 `<urlset`、`llms.txt` 有标题行…） | 成功 |
| 200 但内容为空、或缺预期标记 | 可疑 |
| 404 | 失败（这次构建没生成它） |
| 请求超时 / 网络不通 | **未知**，不算失败 |

> 最后一条是刻意的：**「我查不到」不等于「它挂了」**。探测失败记灰不记红，否则监控机自己网络抖一下就会误报。

三个容易踩空的点，都已经在代码里处理了：

- **一天可能跑很多次**（`save_run` 是追加写）。当天结论取**最后一次**，次数单独显示。
- **`status=success` 但 `item_count=0` 比报错更危险**，因为没人会发现 —— 单独标成「可疑」。
- **今天的缺跑要等宽限期**才判定，否则每天 00:00 到 09:00 都会误报。

### 健康检查位 `/healthz`

三块任务**全部**不处于失败 / 缺跑 / 卡住 → `200`；任一出问题 → `503`，响应里写明是哪块：

```bash
curl -s http://127.0.0.1:8787/healthz
# {"healthy":false,"today":"2026-09-10",
#  "tasks":{"hotsearch":"missing","dm_ingest":"idle","seo_geo":"unknown"},
#  "breaking":["hotsearch"]}
```

「未到点 / 无任务 / 进行中 / 未知」都算健康，不会误报。
可直接拿去做容器健康检查、Uptime Kuma 拨测、nginx 上游探活。

Docker 部署时 `docker-compose.yml` 里已经有一个独立的 `watch` 容器在跑它，
容器状态会直接显示 `healthy` / `unhealthy`：

```bash
docker ps --filter name=jbs-hotsearch-watch
```

> 出榜的（`hotsearch`）和看出榜的（`watch`）刻意分成两个容器 ——
> 出榜挂了的时候，大盘必须还活着。

### 告警（可选）

配了 `HS_WATCH_WEBHOOK_URL` 后，出问题时会 POST 一个 JSON：

```json
{"service": "jbs-hotsearch", "today": "2026-09-09",
 "alerts": [{"date": "2026-09-09", "level": "bad", "message": "米圈 sign 失效：400003"}]}
```

同一个问题只在**状态变化时**推一次，不会每分钟刷屏。留空则不告警。

> 安全提示：页面里含错误信息等内部细节，因为 `script_hot_runs` 的 RLS 只允许 service_role 读，
> 大盘必须带 service_role key 跑。**别把这个端口直接暴露到公网**，要走反代加鉴权，或只在内网 / SSH 隧道里看。

## 7. 排障

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
