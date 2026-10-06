"""独立复现 -R 单向问题，只依赖 paramiko（便于跨版本对比）。

密码从环境变量 PYSSH_PWD 读取。

用法::

    $env:PYSSH_PWD='...'; <python> tests/standalone_remote_forward.py
"""

from __future__ import annotations

import os
import select
import socket
import threading
import time

import paramiko

HOST = "192.168.206.130"
USER = "root"
ECHO_PORT = 18991
REMOTE_PORT = 19088

log = lambda message: print(message, flush=True)
state = {"t0": 0.0}


def echo_server() -> None:
    server = socket.socket()
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind(("127.0.0.1", ECHO_PORT))
    server.listen(4)
    server.settimeout(40)
    log("echo 就绪")

    try:
        while True:
            connection, _ = server.accept()
            connection.settimeout(6)
            connection.sendall(b"BANNER-1\n")

            try:
                data = connection.recv(200)
                log(f"[{time.time() - state['t0']:.2f}s] echo 收到 {data!r}")

                if data:
                    connection.sendall(b"REPLY:" + data)
            except Exception as error:
                log(f"[{time.time() - state['t0']:.2f}s] echo 异常 {error!r}")
            finally:
                connection.close()
    except Exception as error:
        log(f"echo 结束 {error!r}")


def main() -> int:
    password = os.environ.get("PYSSH_PWD", "")

    if not password:
        log("缺少 PYSSH_PWD")
        return 2

    threading.Thread(target=echo_server, daemon=True).start()
    time.sleep(0.3)

    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(HOST, username=USER, password=password, timeout=15, auth_timeout=45, allow_agent=False, look_for_keys=False)
    transport = client.get_transport()
    log(f"paramiko {paramiko.__version__} 已连接")

    def handler(channel, origin, server) -> None:
        log(f"[{time.time() - state['t0']:.2f}s] handler origin={origin}")
        local = socket.create_connection(("127.0.0.1", ECHO_PORT), timeout=6)
        channel.setblocking(False)
        local.setblocking(False)

        try:
            while True:
                readable, _, _ = select.select([channel, local], [], [], 0.5)

                if not readable:
                    if channel.closed:
                        break

                    continue

                if channel in readable:
                    data = channel.recv(65536)
                    log(f"[{time.time() - state['t0']:.2f}s] channel→local {data!r}")

                    if not data:
                        break

                    local.sendall(data)

                if local in readable:
                    data = local.recv(65536)
                    log(f"[{time.time() - state['t0']:.2f}s] local→channel {data!r}")

                    if not data:
                        break

                    channel.sendall(data)
        finally:
            local.close()
            channel.close()

    transport.request_port_forward("127.0.0.1", REMOTE_PORT, handler)
    time.sleep(1.0)

    probe = (
        "P=$(command -v python3 || command -v python); "
        "\"$P\" -c \"import socket,sys,time;p=%d;s=socket.create_connection(('127.0.0.1',p),5);"
        "s.settimeout(5);sys.stdout.write('banner='+repr(s.recv(40)));"
        "s.sendall(b'PING');time.sleep(2.0);"
        "sys.stdout.write(' reply='+repr(s.recv(40)));sys.stdout.flush()\""
    ) % REMOTE_PORT

    state["t0"] = time.time()
    stdin, stdout, stderr = client.exec_command(probe, timeout=30)
    out = stdout.read().decode("utf-8", errors="replace").strip()
    err = stderr.read().decode("utf-8", errors="replace").strip()
    log(f"结果 {out!r} stderr={err[:120]!r}")

    transport.cancel_port_forward("127.0.0.1", REMOTE_PORT)
    client.close()

    ok = "REPLY:PING" in out
    log("STANDALONE_REMOTE_OK" if ok else "STANDALONE_REMOTE_FAILED")

    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
