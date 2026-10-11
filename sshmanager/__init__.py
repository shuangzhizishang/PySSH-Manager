"""PySSH Manager - 一个类似 Termius 的 SSH 管理器。

模块划分：

- ``settings``     应用路径与常量
- ``credentials``  凭据加密存储（Windows DPAPI / Fernet 回退）
- ``hosts``        主机列表持久化（Host / HostStore）
- ``sshclient``    paramiko 封装（认证、known_hosts 校验、跳板机）
- ``terminal``     pyte 终端仿真核心 + Qt 字符网格控件
- ``ui``           主窗口、侧边栏、会话标签、连接对话框
"""

__version__ = "0.2.2"
APP_NAME = "PySSH Manager"
