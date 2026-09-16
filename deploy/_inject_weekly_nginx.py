# -*- coding: utf-8 -*-
"""往 jbs-nginx 的 nginx.conf 里插入 /weekly/ 反代段（幂等，可重复执行）。

用法：本地 scp 到服务器后 python3 执行。不要用 ssh heredoc ——
不带引号的 heredoc 会让远端 bash 把 $host / $remote_addr 展开成空串。
"""
from pathlib import Path
import shutil
import time

p = Path("/opt/jbs/deploy/nginx.conf")
text = p.read_text(encoding="utf-8")

if "location ^~ /weekly/" in text:
    print("SKIPPED: /weekly/ already present")
    raise SystemExit(0)

block = """        # ---- 每周热度周报（jbs-hotsearch 的 weekly，watch 容器 8787）----
        # 公开数据（榜单聚合），与 /reviews/ 同等待遇，不加 basic auth。
        # proxy_pass 末尾必须保留 /weekly/ 前缀，否则 watch 收到 /<file> 而非 /weekly/<file>，404。
        location ^~ /weekly/ {
            proxy_pass http://172.17.0.1:8787/weekly/;
            proxy_http_version 1.1;
            proxy_set_header Connection "";
            proxy_set_header Host $host;
            proxy_set_header X-Real-IP $remote_addr;
            proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
            proxy_set_header X-Forwarded-Proto $scheme;
            proxy_read_timeout 30s;
            proxy_send_timeout 30s;
        }

"""

# 锚定在已有的 /reviews/ 段注释前，插在它上面
anchor = "        # ---- 评论聚合（jbs-hotsearch 的 reviews，watch 容器挂在 8787）----"
assert anchor in text, "anchor not found: /reviews/ comment"
bak = p.with_name(f"nginx.conf.bak.weekly.{time.strftime('%Y%m%d-%H%M%S')}")
shutil.copy2(p, bak)
p.write_text(text.replace(anchor, block + anchor, 1), encoding="utf-8")
print(f"inserted; backup at {bak}")
