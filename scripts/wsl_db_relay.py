# -*- coding: utf-8 -*-
"""WSL 侧 MySQL TCP 中继（纯标准库，零依赖）

为什么需要它：
    treatbord 的 MySQL 跑在 WSL2 里，账号只有 `app_user@'%'`（没有 @'localhost'）。
    Windows 侧经 WSL2 端口转发连进来时，MySQL 把来源解析成 localhost → 匹配不上 → Access denied。
    而在 WSL 内部连接时来源是 127.0.0.1 → 匹配 `%` → 正常。

做法：
    在 WSL 里监听 0.0.0.0:33306，把流量转发到 127.0.0.1:3306。
    Windows 侧连 127.0.0.1:33306 —— 对 MySQL 而言连接来自 WSL 内部，路径与 mysql CLI 完全一致。

用法（在 WSL 里执行，保持常驻）：
    python3 /mnt/e/project/agent/scripts/wsl_db_relay.py            # 前台
    nohup python3 .../wsl_db_relay.py > /tmp/db_relay.log 2>&1 &     # 后台

替代方案（拿到 MySQL root 后更正统）：
    CREATE USER 'agent_ro'@'localhost' IDENTIFIED BY '...';
    GRANT SELECT ON treatbord.* TO 'agent_ro'@'localhost';
    -- 见 sql/readonly_user.sql
"""
import os
import socket
import threading

LISTEN_HOST = os.environ.get("RELAY_LISTEN_HOST", "0.0.0.0")
LISTEN_PORT = int(os.environ.get("RELAY_LISTEN_PORT", "33306"))
UPSTREAM_HOST = os.environ.get("RELAY_UPSTREAM_HOST", "127.0.0.1")
UPSTREAM_PORT = int(os.environ.get("RELAY_UPSTREAM_PORT", "3306"))
BUFSIZE = 65536


def pipe(src: socket.socket, dst: socket.socket) -> None:
    try:
        while True:
            data = src.recv(BUFSIZE)
            if not data:
                break
            dst.sendall(data)
    except OSError:
        pass
    finally:
        for s in (src, dst):
            try:
                s.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass


def handle(client: socket.socket, addr) -> None:
    try:
        upstream = socket.create_connection((UPSTREAM_HOST, UPSTREAM_PORT), timeout=5)
    except OSError as exc:
        print(f"[relay] upstream 连接失败: {exc}", flush=True)
        client.close()
        return
    print(f"[relay] {addr[0]}:{addr[1]} -> {UPSTREAM_HOST}:{UPSTREAM_PORT}", flush=True)
    threading.Thread(target=pipe, args=(client, upstream), daemon=True).start()
    threading.Thread(target=pipe, args=(upstream, client), daemon=True).start()


def main() -> None:
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind((LISTEN_HOST, LISTEN_PORT))
    srv.listen(64)
    print(f"[relay] listening {LISTEN_HOST}:{LISTEN_PORT} -> {UPSTREAM_HOST}:{UPSTREAM_PORT}", flush=True)
    while True:
        client, addr = srv.accept()
        threading.Thread(target=handle, args=(client, addr), daemon=True).start()


if __name__ == "__main__":
    main()
