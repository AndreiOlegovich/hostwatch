"""Client for the host helper (agent.py, outside the container), which reads and mounts disks and USB cards."""

from __future__ import annotations

import http.client
import json
import socket
from pathlib import Path


class HostError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


class _UnixConnection(http.client.HTTPConnection):
    def __init__(self, path: str, timeout: float) -> None:
        super().__init__("localhost", timeout=timeout)
        self.socket_path = path

    def connect(self) -> None:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(self.timeout)
        try:
            sock.connect(self.socket_path)
        except OSError:
            sock.close()
            raise
        self.sock = sock


class HostAgent:
    def __init__(self, socket_path: Path) -> None:
        self.socket_path = socket_path

    def available(self) -> bool:
        return self.socket_path.exists()

    def call(self, method: str, path: str, body: dict[str, object] | None = None, timeout: float = 30) -> dict:
        connection = _UnixConnection(str(self.socket_path), timeout)
        try:
            payload = json.dumps(body).encode() if body is not None else None
            headers = {"Content-Type": "application/json"} if payload else {}
            connection.request(method, path, body=payload, headers=headers)
            response = connection.getresponse()
            data = json.loads(response.read() or b"{}")
        except (OSError, http.client.HTTPException, ValueError) as exc:
            raise HostError(503, f"The host helper is not answering ({exc}). Start it with ./hostwatch.py up") from exc
        finally:
            connection.close()
        if response.status >= 400:
            raise HostError(response.status, str(data.get("detail") or response.reason))
        return data
