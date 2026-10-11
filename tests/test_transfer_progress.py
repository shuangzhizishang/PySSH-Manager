"""传输进度显示的单元测试（百分比 / 速度 / 剩余时间 / 摘要文字）。"""

from __future__ import annotations

from sshmanager.sftpcore import TransferProgress, format_size


# ----------------------------------------------------------------------
# 百分比
# ----------------------------------------------------------------------

def test_percent_from_bytes():
    progress = TransferProgress(task="下载", name="a.rpm", done=3_200_000, total=7_100_000)

    assert progress.percent == 45
    assert "45%" in progress.summary()


def test_percent_unknown_total():
    # 总量未知（远端 stat 失败）时不应显示假的百分比
    assert TransferProgress(done=0, total=0).percent == 0
    assert TransferProgress(done=1234, total=0).percent == 100


def test_percent_clamped():
    assert TransferProgress(done=200, total=100).percent == 100
    assert TransferProgress(done=-5, total=100).percent == 0


def test_batch_percent_prefers_batch_total():
    progress = TransferProgress(done=10, total=100, batch_done=250, batch_total=1000)

    assert progress.percent == 10        # 当前文件
    assert progress.batch_percent == 25  # 整个任务


# ----------------------------------------------------------------------
# 文字
# ----------------------------------------------------------------------

def test_size_text_shows_done_over_total():
    progress = TransferProgress(done=1_048_576, total=2_097_152)

    assert progress.size_text.startswith("1.0 MB/")
    assert "2.0 MB" in progress.size_text


def test_speed_and_eta_text():
    progress = TransferProgress(done=1_000_000, total=5_000_000, batch_done=1_000_000, batch_total=5_000_000, speed=1_000_000)

    assert "/s" in progress.speed_text
    assert progress.eta is not None and 3.9 < progress.eta < 4.1
    assert "4 秒" in progress.eta_text


def test_eta_hidden_without_speed():
    progress = TransferProgress(done=100, total=1000, speed=0)

    assert progress.eta is None
    assert progress.eta_text == ""


def test_eta_minutes_and_hours():
    assert "分" in TransferProgress(done=0, total=600_000, speed=1000).eta_text
    assert "小时" in TransferProgress(done=0, total=36_000_000, speed=1000).eta_text
    assert TransferProgress(done=99, total=100, speed=1000).eta_text == "即将完成"


def test_summary_single_file():
    progress = TransferProgress(task="上传", name="mysql.rpm", done=500, total=1000, speed=100)

    text = progress.summary()

    assert text.startswith("上传 mysql.rpm")
    assert "50%" in text
    assert "第" not in text, "单项传输不该显示第几项"


def test_summary_multiple_items_includes_index_and_total():
    progress = TransferProgress(
        task="下载",
        name="b.rpm",
        done=50,
        total=100,
        batch_done=150,
        batch_total=300,
        index=2,
        count=5,
        speed=10,
    )

    text = progress.summary()

    assert "第 2/5 项" in text
    assert "50%" in text and "总 50%" in text


def test_summary_without_name_has_no_stray_space():
    progress = TransferProgress(task="清理", done=10, total=10)
    text = progress.summary()

    assert text.startswith("清理 · 100%")
    assert "  " not in text and text == text.strip(), "不应出现多余空格"


def test_summary_stays_short_for_narrow_ui():
    progress = TransferProgress(
        task="下载",
        name="very-long-file-name-that-should-not-break-the-layout.rpm",
        done=1,
        total=2,
        speed=1024,
    )

    # UI 会截断，但摘要本身要是一行、不能夹换行
    assert "\n" not in progress.summary()


def test_batch_text_only_when_batch_known():
    assert TransferProgress(batch_done=10, batch_total=0).batch_text == ""
    assert TransferProgress(batch_done=1_048_576, batch_total=2_097_152).batch_text.count("/") == 1


def test_format_size_is_used_consistently():
    progress = TransferProgress(done=1536, total=3072)

    assert format_size(1536) == "1.5 KB"
    assert progress.size_text == "1.5 KB/3.0 KB"
