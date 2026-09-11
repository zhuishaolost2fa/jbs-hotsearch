#!/usr/bin/env bash
# ============================================================
# 服务器部署脚本（Linux 服务器 → Docker Compose 或 systemd，二选一）
#
#   ./deploy/server-deploy.sh docker   up       # 首次部署 / 重建并启动
#   ./deploy/server-deploy.sh docker   update   # 拉代码后重建镜像并重启
#   ./deploy/server-deploy.sh docker   once     # 立刻出一次榜（验证连通性）
#   ./deploy/server-deploy.sh docker   doctor   # 容器里跑自检
#   ./deploy/server-deploy.sh docker   logs     # 看日志
#   ./deploy/server-deploy.sh docker   status   # 容器状态 + 健康检查
#
#   ./deploy/server-deploy.sh systemd  install  # 裸机部署：venv + systemd 常驻
#   ./deploy/server-deploy.sh systemd  update|logs|status|once|doctor
#
# 环境变量：APP_DIR（默认 /opt/jbs-hotsearch）
# ============================================================
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/jbs-hotsearch}"
MODE="${1:-docker}"
ACTION="${2:-up}"

cd "$APP_DIR"

die() { echo "✗ $*" >&2; exit 1; }

need_env() {
  [[ -f .env ]] || die "缺 $APP_DIR/.env —— 先 cp .env.example .env，把 SUPABASE_URL / SERVICE_ROLE_KEY 填好"
}

compose_cmd() {
  if docker compose version >/dev/null 2>&1; then echo "docker compose";
  elif command -v docker-compose >/dev/null 2>&1; then echo "docker-compose";
  else die "没找到 docker compose，先装 Docker: https://docs.docker.com/engine/install/"
  fi
}

docker_init() {
  need_env
  local DC; DC="$(compose_cmd)"
  mkdir -p data/reports data/snapshots
  $DC build
  $DC up -d
  $DC ps
  echo
  echo "✓ 已启动。看日志：$0 docker logs；验证一次出榜：$0 docker once"
}

docker_update() {
  need_env
  local DC; DC="$(compose_cmd)"
  # 刻意不用 `--pull`：
  # 1) 服务器访问不了 Docker Hub（registry-1.docker.io 直接超时，国内机器常态），
  #    `--pull` 会先卡在回源拉基础镜像上，几分钟后连接超时，构建一起被杀；
  # 2) 它会重新解析基础镜像 digest，digest 一变整条缓存链失效，
  #    `playwright install --with-deps chromium` 那层要重跑十几分钟。
  # 基础镜像真的要更新时手动 `docker compose build --pull` 一次即可。
  $DC build
  $DC up -d
  $DC ps
}

docker_exec() { # once / doctor
  need_env
  docker compose run --rm hotsearch hotsearch "$1" || \
    docker-compose run --rm hotsearch hotsearch "$1" || \
    docker exec jbs-hotsearch hotsearch "$1"
}

systemd_cmd_available() {
  command -v systemctl >/dev/null 2>&1 || die "这台机器没有 systemd，请用 docker 模式"
}

systemd_install() {
  systemd_cmd_available
  need_env
  command -v python3 >/dev/null 2>&1 || die "缺 python3"
  [[ -d .venv ]] || python3 -m venv .venv
  ./.venv/bin/pip install --upgrade pip
  ./.venv/bin/pip install .
  sed "s#/opt/jbs-hotsearch#$APP_DIR#g" deploy/jbs-hotsearch.service \
    | tee /etc/systemd/system/jbs-hotsearch.service >/dev/null
  # 监听大盘单独一个 unit：出榜的挂了时，大盘必须还活着
  sed "s#/opt/jbs-hotsearch#$APP_DIR#g" deploy/jbs-hotsearch-watch.service \
    | tee /etc/systemd/system/jbs-hotsearch-watch.service >/dev/null
  systemctl daemon-reload
  systemctl enable jbs-hotsearch jbs-hotsearch-watch
  systemctl restart jbs-hotsearch jbs-hotsearch-watch
  systemctl --no-pager status jbs-hotsearch jbs-hotsearch-watch | head -24
}

systemd_update() {
  systemd_cmd_available
  ./.venv/bin/pip install .
  systemctl restart jbs-hotsearch jbs-hotsearch-watch
  systemctl --no-pager status jbs-hotsearch jbs-hotsearch-watch | head -24
}

systemd_run() { # once / doctor
  ./.venv/bin/hotsearch "$1"
}

case "$MODE" in
  docker)
    case "$ACTION" in
      up)      docker_init ;;
      update)  docker_update ;;
      once)    docker_exec once ;;
      doctor)  docker_exec doctor ;;
      logs)    docker compose logs -f --tail 100 || docker-compose logs -f --tail 100 ;;
      status)  docker ps --filter name=jbs-hotsearch ;;
      *)       die "未知操作：$ACTION" ;;
    esac
    ;;
  systemd)
    case "$ACTION" in
      install|up) systemd_install ;;
      update)     systemd_update ;;
      once)       systemd_run once ;;
      doctor)     systemd_run doctor ;;
      logs)       journalctl -u jbs-hotsearch -u jbs-hotsearch-watch -f --since today ;;
      status)     systemctl --no-pager status jbs-hotsearch jbs-hotsearch-watch | head -24 ;;
      *)          die "未知操作：$ACTION" ;;
    esac
    ;;
  *) die "未知模式：$MODE（docker / systemd）" ;;
esac
