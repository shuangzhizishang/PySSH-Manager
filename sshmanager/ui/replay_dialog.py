"""录制回放对话框：按时间轴重放 asciicast v2 录制。"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSlider,
    QVBoxLayout,
)

from ..recording import CastRecording, default_recordings_dir, load_recording
from ..terminal.view import TerminalView


class ReplayDialog(QDialog):
    """加载一份录制并按原始时间轴播放。"""

    SPEEDS = (0.5, 1.0, 2.0, 4.0)

    def __init__(self, path: Path | None = None, parent=None) -> None:
        super().__init__(parent)

        self.recording: CastRecording | None = None
        self._position = 0.0
        self._playhead = 0.0
        self._timer = QTimer(self)
        self._timer.setInterval(30)
        self._timer.timeout.connect(self._tick)

        self.setWindowTitle("会话回放")
        self.resize(1000, 640)

        self._build_ui()

        if path is not None:
            self.load(path)

    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        column = QVBoxLayout(self)

        bar = QHBoxLayout()

        self.open_button = QPushButton("打开录制…")
        self.open_button.clicked.connect(self.choose_file)

        self.play_button = QPushButton("播放")
        self.play_button.clicked.connect(self.toggle_play)
        self.play_button.setEnabled(False)

        self.restart_button = QPushButton("重头播放")
        self.restart_button.clicked.connect(self.restart)
        self.restart_button.setEnabled(False)

        self.speed_combo = QComboBox()

        for speed in self.SPEEDS:
            self.speed_combo.addItem(f"{speed:g}×", speed)

        self.speed_combo.setCurrentIndex(1)

        self.info = QLabel("请选择一份录制文件")
        self.info.setStyleSheet("color: #8b949e;")

        bar.addWidget(self.open_button)
        bar.addWidget(self.play_button)
        bar.addWidget(self.restart_button)
        bar.addWidget(QLabel("速度"))
        bar.addWidget(self.speed_combo)
        bar.addStretch()
        bar.addWidget(self.info)

        column.addLayout(bar)

        self.view = TerminalView(columns=120, rows=32)
        column.addWidget(self.view, 1)

        timeline = QHBoxLayout()

        self.slider = QSlider(Qt.Orientation.Horizontal)
        self.slider.setRange(0, 1000)
        self.slider.sliderMoved.connect(self._on_seek)
        self.slider.setEnabled(False)

        self.time_label = QLabel("0.0s / 0.0s")

        timeline.addWidget(self.slider, 1)
        timeline.addWidget(self.time_label)

        column.addLayout(timeline)

        box = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        box.rejected.connect(self.reject)
        box.accepted.connect(self.accept)
        column.addWidget(box)

    # ------------------------------------------------------------------

    def choose_file(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self,
            "打开录制文件",
            str(default_recordings_dir()),
            "asciicast 录制 (*.cast);;所有文件 (*)",
        )

        if path:
            self.load(Path(path))

    def load(self, path: Path) -> bool:
        """加载录制文件；已加载时把画面重置到开头。"""

        self._timer.stop()

        try:
            self.recording = load_recording(path)
        except Exception as error:  # noqa: BLE001
            self.info.setText(f"读取失败：{error}")
            return False

        self.view.emulator.resize(self.recording.width, self.recording.height)
        self.view.viewport().update()

        self._position = 0.0
        self._playhead = 0.0
        self.view.emulator.screen.reset()

        total = max(0.1, self.recording.duration)
        self.slider.setEnabled(True)
        self.play_button.setEnabled(True)
        self.restart_button.setEnabled(True)
        self.slider.setRange(0, 1000)
        self.slider.setValue(0)

        self.info.setText(
            f"{path.name} · {self.recording.width}×{self.recording.height} · "
            f"{total:.1f}s · {len(self.recording.events)} 事件"
        )
        self._update_time_label()

        return True

    # ------------------------------------------------------------------

    def toggle_play(self) -> None:
        if self._timer.isActive():
            self._timer.stop()
            self.play_button.setText("继续")
            return

        if self.recording is None:
            return

        if self._playhead >= self.recording.duration:
            self.restart()
            return

        self._timer.start()
        self.play_button.setText("暂停")

    def restart(self) -> None:
        if self.recording is None:
            return

        self._timer.stop()
        self._playhead = 0.0
        self.view.emulator.screen.reset()
        self.view.viewport().update()
        self.slider.setValue(0)
        self._update_time_label()

        self._timer.start()
        self.play_button.setText("暂停")

    def _on_seek(self, value: int) -> None:
        if self.recording is None:
            return

        was_playing = self._timer.isActive()
        self._timer.stop()
        target = self.recording.duration * value / 1000.0

        # 从头快放到目标位置
        self.view.emulator.screen.reset()
        self._playhead = 0.0

        for event in self.recording.events:
            if event.offset > target:
                break

            if event.kind == "o":
                self.view.feed(event.data.encode("utf-8", errors="replace"))

            self._playhead = event.offset

        self.view.viewport().update()
        self._update_time_label()

        if was_playing:
            self._timer.start()

    # ------------------------------------------------------------------

    def _tick(self) -> None:
        if self.recording is None:
            return

        speed = self.speed_combo.currentData() or 1.0
        self._playhead += self._timer.interval() / 1000.0 * speed

        for event in self.recording.events:
            if event.offset <= self._position or event.offset > self._playhead:
                continue

            if event.kind == "o":
                self.view.feed(event.data.encode("utf-8", errors="replace"))

        self._position = self._playhead

        if self._playhead >= self.recording.duration:
            self._timer.stop()
            self.play_button.setText("重头播放")

        if self.recording.duration > 0:
            self.slider.blockSignals(True)
            self.slider.setValue(int(self._playhead / self.recording.duration * 1000))
            self.slider.blockSignals(False)

        self._update_time_label()

    def _update_time_label(self) -> None:
        total = self.recording.duration if self.recording else 0.0
        self.time_label.setText(f"{self._playhead:.1f}s / {total:.1f}s")

    # ------------------------------------------------------------------

    def closeEvent(self, event):  # noqa: N802
        self._timer.stop()
        event.accept()
