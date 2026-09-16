#!/usr/bin/env bash
# 幂等：确保 jbs-nginx 容器内存在 /watch/ 的 basic auth 口令文件。
#
# 背景：/watch/ 走 basic auth，口令文件是 docker cp 注入到容器内的
# /etc/nginx/watch.htpasswd（不是 bind mount）。jbs-nginx 容器一旦重建，
# 该文件就丢失 —— 表现是所有请求被 nginx 以 500 拒绝（不是 401），
# 页面看起来就是"挂了"。
#
# 用法：
#   ./deploy/ensure-watch-htpasswd.sh            # 缺失才注入并 reload
#   NGINX_CONTAINER=jbs-nginx ./deploy/...       # 指定容器名
#   ./deploy/...  /path/to/other.htpasswd        # 指定口令文件源
#
# 建议用 root cron 兜底（最多 10 分钟自愈）：
#   */10 * * * * /opt/jbs-hotsearch/deploy/ensure-watch-htpasswd.sh \
#       >> /var/log/jbs-watch-htpasswd.log 2>&1
set -euo pipefail

SRC="${1:-/opt/jbs-hotsearch/deploy/nginx-watch.htpasswd}"
CONTAINER="${NGINX_CONTAINER:-jbs-nginx}"
DEST="/etc/nginx/watch.htpasswd"

if [ ! -f "$SRC" ]; then
  echo "[$(date -Is)] 口令文件源不存在：$SRC" >&2
  exit 1
fi

if ! docker inspect "$CONTAINER" >/dev/null 2>&1; then
  echo "[$(date -Is)] 容器 $CONTAINER 未运行，跳过"
  exit 0
fi

if docker exec "$CONTAINER" test -s "$DEST"; then
  exit 0
fi

docker cp "$SRC" "$CONTAINER:$DEST"
if docker exec "$CONTAINER" nginx -t >/dev/null 2>&1; then
  docker exec "$CONTAINER" nginx -s reload
  echo "[$(date -Is)] 已注入 $CONTAINER:$DEST 并 reload"
else
  echo "[$(date -Is)] 已注入 $CONTAINER:$DEST，但 nginx -t 失败，未 reload" >&2
  exit 1
fi
