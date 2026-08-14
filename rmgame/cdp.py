# -*- coding: utf-8 -*-
"""最小 CDP（Chrome DevTools Protocol）客户端 —— rmgame/cdp

职责（设计文档 §4.4）：MV/MZ 的 Game.exe 是 nw.js 运行时，可带
`--remote-debugging-port` 启动后用 CDP 读取游戏内部状态。

实现约束：仅标准库（socket + hashlib + base64 + json），零第三方依赖。
功能边界：
- HTTP 探测 /json/version（端口是否已开调试）与 /json/list（取调试页
  WebSocket 地址）
- RFC 6455 客户端握手 + 文本帧收发（仅支持单帧文本消息 —— CDP
  Runtime.evaluate 的响应通常单帧，足够本工具使用）
- Runtime.evaluate 执行 JS 表达式并返回 `returnByValue` 结果

限制（文档 R3 对策）：不处理分片/二进制帧/ping 自动应答；MV/MZ 内部
全局对象版本差异由调用方（monitor.py）的表达式适配。
"""

import base64
import hashlib
import json
import os
import socket
import urllib.request
from urllib.parse import urlparse

WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


# ---------------------------------------------------------------------------
# HTTP 探测（/json 端点）
# ---------------------------------------------------------------------------

def cdp_version(port: int, timeout: float = 0.5) -> dict or None:
    """探测 127.0.0.1:<port>/json/version；不可用返回 None。"""
    try:
        with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/json/version", timeout=timeout) as r:
            if r.status != 200:
                return None
            return json.loads(r.read().decode("utf-8"))
    except (OSError, json.JSONDecodeError, ValueError):
        return None


def cdp_page_url(port: int, timeout: float = 0.5) -> str:
    """取第一个调试页（page）的 webSocketDebuggerUrl。

    找不到 page 时尝试其它 target（backgrond_page 等）；无任何 target 抛
    ConnectionError。
    """
    try:
        with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/json/list", timeout=timeout) as r:
            targets = json.loads(r.read().decode("utf-8"))
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        raise ConnectionError(f"CDP /json/list 不可用: {exc}")
    for t in targets:
        if t.get("type") == "page" and t.get("webSocketDebuggerUrl"):
            return t["webSocketDebuggerUrl"]
    for t in targets:
        if t.get("webSocketDebuggerUrl"):
            return t["webSocketDebuggerUrl"]
    raise ConnectionError("CDP 无可用调试 target")


# ---------------------------------------------------------------------------
# WebSocket 客户端（RFC 6455，最小实现）
# ---------------------------------------------------------------------------

def _ws_connect(ws_url: str, timeout: float = 2.0):
    """握手；返回已连接 socket。"""
    p = urlparse(ws_url)
    host, port = p.hostname, p.port or 80
    path = p.path or "/"
    s = socket.create_connection((host, port), timeout=timeout)
    key = base64.b64encode(os.urandom(16)).decode()
    req = (f"GET {path} HTTP/1.1\r\n"
           f"Host: {host}:{port}\r\n"
           "Upgrade: websocket\r\n"
           "Connection: Upgrade\r\n"
           f"Sec-WebSocket-Key: {key}\r\n"
           "Sec-WebSocket-Version: 13\r\n\r\n")
    s.sendall(req.encode("latin1"))
    resp = b""
    while b"\r\n\r\n" not in resp:
        chunk = s.recv(4096)
        if not chunk:
            raise ConnectionError("WebSocket 握手失败：连接关闭")
        resp += chunk
    head = resp.decode("latin1", "replace")
    status_line = head.split("\r\n", 1)[0]
    if "101" not in status_line:
        raise ConnectionError(f"WebSocket 握手被拒绝: {status_line}")
    # 校验 Sec-WebSocket-Accept（可选，做完整性检查）
    expect = base64.b64encode(
        hashlib.sha1((key + WS_GUID).encode()).digest()).decode()
    if expect not in head:
        raise ConnectionError("WebSocket 握手校验失败（Accept 不匹配）")
    return s


def _send_text(s, payload: str) -> None:
    """发送文本帧（client → server 必须掩码）。"""
    data = payload.encode("utf-8")
    mask = os.urandom(4)
    n = len(data)
    header = bytearray([0x81])  # FIN + opcode=text
    if n < 126:
        header.append(0x80 | n)
    elif n < 65536:
        header.append(0x80 | 126)
        header += n.to_bytes(2, "big")
    else:
        header.append(0x80 | 127)
        header += n.to_bytes(8, "big")
    masked = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
    s.sendall(bytes(header) + mask + masked)


def _recv_text(s, timeout: float = 10.0) -> str:
    """读一个文本帧；忽略 ping/pong/非文本帧。"""
    s.settimeout(timeout)

    def read_n(n):
        buf = b""
        while len(buf) < n:
            chunk = s.recv(n - len(buf))
            if not chunk:
                raise ConnectionError("WebSocket 连接关闭")
            buf += chunk
        return buf

    while True:
        hdr = read_n(2)
        opcode = hdr[0] & 0x0F
        masked = hdr[1] & 0x80
        n = hdr[1] & 0x7F
        if n == 126:
            n = int.from_bytes(read_n(2), "big")
        elif n == 127:
            n = int.from_bytes(read_n(8), "big")
        mask = read_n(4) if masked else None
        payload = read_n(n)
        if mask:
            payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        if opcode == 0x8:      # close
            raise ConnectionError("WebSocket 收到 close 帧")
        if opcode == 0x1:      # text
            return payload.decode("utf-8", "replace")
        # 0x9/0xA ping/pong 忽略；分片（0x0）未实现 → 文档限制


# ---------------------------------------------------------------------------
# Runtime.evaluate
# ---------------------------------------------------------------------------

def cdp_evaluate(ws_url: str, expression: str,
                 timeout: float = 3.0) -> str:
    """执行 JS 表达式，返回 returnByValue 结果（JSON 字符串）。

    expression 应为"可 JSON 序列化"的表达式（如 JSON.stringify({...})）；
    返回值是结果 value 的 JSON 文本；异常时抛 ConnectionError。
    """
    s = _ws_connect(ws_url)
    try:
        msg = json.dumps({
            "id": 1,
            "method": "Runtime.evaluate",
            "params": {"expression": expression, "returnByValue": True},
        })
        _send_text(s, msg)
        while True:
            payload = _recv_text(s, timeout=timeout)
            try:
                resp = json.loads(payload)
            except json.JSONDecodeError:
                continue
            if resp.get("id") != 1:
                continue
            if "error" in resp:
                raise ConnectionError(f"CDP 执行失败: {resp['error']}")
            result = resp.get("result", {}).get("result", {})
            if "value" not in result:
                raise ConnectionError(
                    f"CDP 无返回值（可能表达式异常）: {result}")
            return json.dumps(result["value"], ensure_ascii=False)
    finally:
        try:
            s.close()
        except OSError:
            pass
