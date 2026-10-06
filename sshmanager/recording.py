"""会话录制与回放（asciicast v2 格式）。

录制文件是 UTF-8 文本，每行一个 JSON 对象：:

    {"version": 2, "width": 120, "height": 32, "timestamp": 1234567890, "env": {"TERM": "xterm-256color"}}
    [0.123456, "o", "输出文本"]
    [0.234567, "i", "输入文本"]

这是 asciinema 的通用格式，录下来的文件可以直接用 ``asciinema play`` 播放，
也可以在本应用的回放器里播放。
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path

HEADER_VERSION = 2


@dataclass
class CastEvent:
    """一条录制事件。"""

    offset: float
    kind: str  # "o" 输出 / "i" 输入 / "r" 尺寸变化
    data: str


@dataclass
class CastRecording:
    """一份录制内容。"""

    width: int = 120
    height: int = 32
    timestamp: float = field(default_factory=time.time)
    title: str = ""
    env: dict = field(default_factory=dict)
    events: list[CastEvent] = field(default_factory=list)

    @property
    def duration(self) -> float:
        return self.events[-1].offset if self.events else 0.0

    @property
    def output_bytes(self) -> int:
        return sum(len(event.data) for event in self.events if event.kind == "o")


class CastRecorder:
    """把会话输出/输入写成 asciicast v2 文件。"""

    def __init__(self, path: Path, width: int = 120, height: int = 32, title: str = "") -> None:
        self.path = Path(path)
        self.width = max(2, int(width))
        self.height = max(2, int(height))
        self.title = title

        self._handle = None
        self._start = 0.0
        self._events = 0
        self._bytes = 0

    # ------------------------------------------------------------------

    @property
    def active(self) -> bool:
        return self._handle is not None

    @property
    def event_count(self) -> int:
        return self._events

    @property
    def byte_count(self) -> int:
        return self._bytes

    @property
    def elapsed(self) -> float:
        return time.time() - self._start if self._start else 0.0

    # ------------------------------------------------------------------

    def start(self) -> None:
        if self._handle is not None:
            return

        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._handle = self.path.open("w", encoding="utf-8", newline="\n")
        self._start = time.time()

        header = {
            "version": HEADER_VERSION,
            "width": self.width,
            "height": self.height,
            "timestamp": int(self._start),
            "env": {"TERM": "xterm-256color", "SHELL": "PySSH Manager"},
        }

        if self.title:
            header["title"] = self.title

        self._write_line(header)

    def write_output(self, data: bytes | str) -> None:
        self._append("o", data)

    def write_input(self, data: bytes | str) -> None:
        self._append("i", data)

    def write_resize(self, width: int, height: int) -> None:
        if self._handle is None:
            return

        self.width = max(2, int(width))
        self.height = max(2, int(height))
        self._append("r", f"{self.width}x{self.height}")

    def stop(self) -> Path | None:
        if self._handle is None:
            return None

        try:
            self._handle.flush()
            self._handle.close()
        finally:
            self._handle = None

        return self.path

    # ------------------------------------------------------------------

    def _append(self, kind: str, data: bytes | str) -> None:
        if self._handle is None:
            return

        text = data.decode("utf-8", errors="replace") if isinstance(data, bytes) else data

        if not text:
            return

        self._write_line([round(time.time() - self._start, 6), kind, text])
        self._events += 1
        self._bytes += len(text)

    def _write_line(self, payload) -> None:
        if self._handle is None:
            return

        self._handle.write(json.dumps(payload, ensure_ascii=False) + "\n")


def load_recording(path: Path) -> CastRecording:
    """读取 asciicast v2 文件。"""

    path = Path(path)
    recording = CastRecording()
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()

    for index, line in enumerate(lines):
        line = line.strip()

        if not line:
            continue

        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue

        if index == 0 and isinstance(payload, dict):
            recording.width = int(payload.get("width") or 120)
            recording.height = int(payload.get("height") or 32)
            recording.timestamp = float(payload.get("timestamp") or time.time())
            recording.title = payload.get("title") or ""
            recording.env = payload.get("env") or {}
            continue

        if isinstance(payload, list) and len(payload) >= 3:
            recording.events.append(CastEvent(offset=float(payload[0]), kind=str(payload[1]), data=str(payload[2])))

    return recording


def recording_text(path: Path) -> str:
    """把录制里的输出拼成纯文本（便于快速查看/搜索）。"""

    recording = load_recording(path)

    return "".join(event.data for event in recording.events if event.kind == "o")


def default_recordings_dir() -> Path:
    from . import settings

    path = settings.data_dir() / "recordings"
    path.mkdir(parents=True, exist_ok=True)

    return path


def suggest_filename(title: str) -> str:
    """按主机名与时间生成一个录制文件名。"""

    safe = "".join(char if char.isalnum() or char in "-_." else "_" for char in (title or "session"))[:40]

    return f"{safe or 'session'}-{time.strftime('%Y%m%d-%H%M%S')}.cast"
