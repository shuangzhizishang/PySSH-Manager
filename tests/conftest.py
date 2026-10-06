"""测试公共配置：把数据目录指向临时目录，避免污染真实 AppData。"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

# 必须在导入 sshmanager 之前设置，因为 settings 在导入期不读值、调用时才读，
# 但 HostStore/CredentialStore 的默认构造会立刻落盘，所以这里也提前设好。
_TEMP_DIR = Path(tempfile.mkdtemp(prefix="pyssh-tests-"))
os.environ.setdefault("PYSSH_DATA_DIR", str(_TEMP_DIR))

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest  # noqa: E402


@pytest.fixture()
def data_dir(tmp_path, monkeypatch) -> Path:
    """每条测试一个独立的数据目录。"""

    target = tmp_path / "data"
    target.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("PYSSH_DATA_DIR", str(target))

    return target
