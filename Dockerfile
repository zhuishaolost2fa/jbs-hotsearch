# 每日热门榜服务：单进程常驻，内置调度（每天 HS_RUN_AT 触发）
FROM python:3.12-slim

# deb.debian.org 在国内直连极慢：实测单包 20~30 秒，光 `playwright install --with-deps`
# 这一层就要跑一小时以上（构建中途还会因为长连接被掐断而前功尽弃）。
# 默认切到腾讯云镜像；境外构建时用 --build-arg APT_MIRROR=deb.debian.org 还原。
# 注意改的是镜像内的文件，所以后面 `playwright install --with-deps` 的 apt 也一起受益。
ARG APT_MIRROR=mirrors.cloud.tencent.com
RUN set -eux; \
    sed -i "s#//deb.debian.org#//${APT_MIRROR}#g" /etc/apt/sources.list.d/debian.sources 2>/dev/null || true; \
    sed -i "s#//deb.debian.org#//${APT_MIRROR}#g" /etc/apt/sources.list 2>/dev/null || true

# python:slim 不带 tzdata —— 没有它 zoneinfo("Asia/Shanghai") 直接抛错，
# 服务会被 tz_util 兜底成固定 +08:00（能用但无法感知夏令时以外的任何时区配置），这里补上。
RUN apt-get update \
    && apt-get install -y --no-install-recommends tzdata ca-certificates fonts-noto-cjk \
    && rm -rf /var/lib/apt/lists/*

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TZ=Asia/Shanghai \
    HS_DATA_DIR=/app/data \
    HS_SOURCE_MIQUAN_CURLS_FILE=/app/data/miquan_curls.txt \
    PIP_INDEX_URL=https://mirrors.cloud.tencent.com/pypi/simple \
    PIP_TRUSTED_HOST=mirrors.cloud.tencent.com \
    PLAYWRIGHT_DOWNLOAD_HOST=https://cdn.npmmirror.com/binaries/playwright

LABEL org.opencontainers.image.title="jbs-hotsearch" \
      org.opencontainers.image.description="剧本杀每日热门榜 Top10（内置每日调度）" \
      org.opencontainers.image.source="https://github.com/zhuishaolost2fa/jbs-hotsearch"

WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# 安装 chromium 无头浏览器及其系统依赖（小红书素材海报截图用）
RUN playwright install --with-deps chromium

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
