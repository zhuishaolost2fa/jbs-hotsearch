# 每日热门榜服务：单进程常驻，内置调度（每天 HS_RUN_AT 触发）
FROM python:3.12-slim

# python:slim 不带 tzdata —— 没有它 zoneinfo("Asia/Shanghai") 直接抛错，
# 服务会被 tz_util 兜底成固定 +08:00（能用但无法感知夏令时以外的任何时区配置），这里补上。
RUN apt-get update \
    && apt-get install -y --no-install-recommends tzdata ca-certificates \
    && rm -rf /var/lib/apt/lists/*

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TZ=Asia/Shanghai \
    HS_DATA_DIR=/app/data \
    HS_SOURCE_MIQUAN_CURLS_FILE=/app/data/miquan_curls.txt

LABEL org.opencontainers.image.title="jbs-hotsearch" \
      org.opencontainers.image.description="剧本杀每日热门榜 Top10（内置每日调度）" \
      org.opencontainers.image.source="https://github.com/zhuishaolost2fa/jbs-hotsearch"

WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY pyproject.toml ./
COPY src ./src
RUN pip install --no-cache-dir .

# 抓包文件与本地快照/报告留在 volume 里，重建容器不丢
VOLUME ["/app/data"]

# 健康检查：最近 48 小时内出过报告 = 服务还活着（这条命令只用 python，不依赖 find/grep）。
# 它只能说明「有产出」；榜算错没算错要看 Supabase 的 script_hot_runs.status。
HEALTHCHECK --interval=30m --timeout=15s --start-period=5m --retries=3 \
    CMD python -c "import glob,os,sys,time;f=glob.glob('/app/data/reports/*.md');sys.exit(0 if f and time.time()-max(map(os.path.getmtime,f))<172800 else 1)"

CMD ["hotsearch", "serve"]
