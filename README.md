# PySSH Manager

一个用 Python + PySide6 写的 SSH 连接管理器，目标是对齐 [Termius](https://termius.com/) 的核心体验：
真终端仿真、多标签会话、主机列表与加密凭据、密钥认证、主机指纹校验。

## 当前状态

已经可用的功能：

- **真终端仿真**：基于 `pyte` 解析 ANSI/SGR/光标/OSC，`vim`、`top` 这类全屏程序可用
- **字符网格渲染**：自绘终端控件，支持 16 色 + 亮色、加粗/斜体/下划线/反显、块状光标
- **回滚历史**：向上滚动可回看历史输出（鼠标滚轮 / 滚动条）
- **多标签会话**：一个主机一个标签，标签标题跟随远端 OSC 设置
- **主机列表**：JSON 持久化，按最近连接排序
- **凭据加密**：Windows 用系统 DPAPI 加密（`CryptProtectData`），不落明文；其他平台回退 Fernet
- **认证方式**：密码、私钥文件（含 passphrase）
- **主机指纹校验**：使用应用自己的 `known_hosts`，未知主机给出带指纹的明确提示，可一键信任
- **跳板机**：通过另一台主机建立 direct-tcpip 隧道
- **SFTP 文件管理**：双栏（本地 / 远端）浏览，上传、下载、新建目录、重命名、删除，
  传输带进度条与取消
- **本地端口转发**：图形化管理规则（等价 `ssh -L`），支持启停与状态显示
- **快捷键**：`Ctrl+N` 新建、`Ctrl+Enter` 连接、`Ctrl+R` 重连、`Ctrl+W` 关标签、
  `Ctrl+Shift+F` 打开 SFTP、`Ctrl+Shift+P` 打开端口转发、
  `Ctrl+Shift+C/V` 复制粘贴、`Ctrl+ +/-/0` 字体缩放、`Ctrl+鼠标滚轮` 缩放

尚未实现（后续计划）：会话分屏、主机导入导出、本地终端、远端转发（`ssh -R`）与动态转发（SOCKS）。

## 快速开始

```powershell
# 依赖（虚拟环境已就绪时跳过）
.venv\Scripts\python.exe -m pip install -r requirements.txt

# 打开界面（默认连 192.168.206.130）
.venv\Scripts\python.exe main.py

# 指定主机，并顺手把密码加密保存
.venv\Scripts\python.exe main.py --host 10.0.0.5 --username root --password secret

# 只打开主机列表，不自动连接
.venv\Scripts\python.exe main.py --no-autoconnect
```

在 PyCharm 里直接运行 `main.py` 即可，工作目录设为项目根目录。

数据文件位置：`%APPDATA%\PySSH-Manager\`

- `hosts.json`：主机列表（不含密码）
- `credentials.json`：DPAPI 加密后的密码/口令
- `known_hosts`：本应用记录的主机公钥

## 项目结构

```
main.py                     启动入口（命令行参数、Fusion 样式）
sshmanager/
  settings.py               路径与常量
  credentials.py            凭据加密存储（DPAPI / Fernet）
  hosts.py                  Host 数据模型 + HostStore 持久化
  sshclient.py              paramiko 封装：认证、指纹校验、PTY、SFTP
  session.py                SSHSession（QThread）：连接、读取、断开
  sftpcore.py               SFTP 操作（列目录/收发/增删改，不依赖 Qt）
  sftpworker.py             SFTP 后台线程（任务队列）
  forwarding.py             本地端口转发（ssh -L 机制）
  terminal/
    core.py                 pyte 封装 + 键盘序列编码
    view.py                 终端控件：网格渲染、选区、滚动、快捷键
  ui/
    main_window.py          主窗口：主机侧边栏 + 多标签（终端 / SFTP）
    host_dialog.py          新建/编辑主机（含「信任并记住指纹」）
    session_tab.py          单个会话标签
    sftp_panel.py           SFTP 双栏面板
    forward_dialog.py       端口转发管理对话框
tests/
  test_terminal.py          终端仿真核心单测
  test_terminal_view.py     滚动与排版回归测试
  test_storage.py           主机存储与凭据加密单测
  test_sftp_forward.py      SFTP 辅助函数与转发规则单测
  gui_smoke.py              离屏 GUI 冒烟（不需要联网）
  ssh_smoke.py              真实 SSH 冒烟（需要可达的服务器）
  gui_e2e.py                真实 GUI 端到端 + 截图
  sftp_forward_smoke.py     SFTP + 端口转发真实链路验证
  sftp_gui_e2e.py           SFTP 面板与转发对话框 GUI 验证
  key_auth_smoke.py         密钥认证验证（本地起 paramiko 服务器，不出网）
```

## 测试

```powershell
# 单元测试（离屏，不需要网络）
$env:QT_QPA_PLATFORM='offscreen'
.venv\Scripts\python.exe -m pytest tests -q

# 离屏 GUI 冒烟
.venv\Scripts\python.exe tests\gui_smoke.py

# 真实 SSH 冒烟（连测试机执行命令并校验 ANSI 解析）
.venv\Scripts\python.exe tests\ssh_smoke.py --host 192.168.206.130 --trust

# 真实 GUI 端到端（会短暂弹窗，并把截图写到 build/）
.venv\Scripts\python.exe tests\gui_e2e.py --host 192.168.206.130 --password <密码>

# SFTP + 端口转发真实链路（上传/下载/重命名/删除 + ssh -L 回传校验）
.venv\Scripts\python.exe tests\sftp_forward_smoke.py --host 192.168.206.130 --password <密码>

# SFTP 面板与端口转发对话框的 GUI 验证
.venv\Scripts\python.exe tests\sftp_gui_e2e.py --host 192.168.206.130 --password <密码>

# 密钥认证（本地 paramiko 服务器，不需要外网）
.venv\Scripts\python.exe tests\key_auth_smoke.py
```

## 三个已踩过的坑（改动时注意）

1. **必须使用 Fusion 样式**：Qt 6.11 在 Windows 上默认用 `windows11` 原生样式，
   该样式会忽略自定义 `QPainter` 的画笔颜色，终端文字会被画成系统默认文字色，
   在深色背景上几乎不可见（表现为"终端一片空白"）。所以在 `main.py` 里显式
   `app.setStyle("Fusion")`。
2. **回滚历史交给 pyte**：不要自己逐行比对去推断"滚了几行"。早期实现这样猜，
   每次输出都会误判并累加 `_offset`，最终把整个画面推出视口。现在直接用
   `HistoryScreen.history.top`，`_offset` 只由滚动条驱动。
3. **本地转发不能直接用 `request_port_forward`**：paramiko 的
   `Transport.request_port_forward()` 是**远端转发**（让服务器监听、把连接送回客户端，
   即 `ssh -R`）。`ssh -L` 必须本进程监听 socket，收到连接后再用 `direct-tcpip`
   通道桥接，见 `sshmanager/forwarding.py`。
