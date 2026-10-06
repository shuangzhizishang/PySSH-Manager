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
- **本地终端**：应用内嵌 Windows ConPTY 伪终端（PowerShell / cmd），标签里直接跑本机命令
- **分屏**：左右 / 上下分屏，最多 4 个面板，每个面板是独立的 SSH 会话
- **SFTP 文件管理**：双栏（本地 / 远端）浏览，上传、下载、新建目录、重命名、删除，
  传输带进度条与取消
- **端口转发（三类）**：
  - 本地转发 `ssh -L`：本机端口 → 服务器可达的目标
  - 远端转发 `ssh -R`：服务器监听端口 → 本机服务
  - 动态转发 `ssh -D`：本机 SOCKS5 代理
  规则随主机持久化，可启停并显示状态
- **主机导入导出**：OpenSSH `~/.ssh/config` 与 JSON 两种格式（JSON 可选带上加密凭据）
- **快捷键**：`Ctrl+N` 新建、`Ctrl+Enter` 连接、`Ctrl+R` 重连、`Ctrl+W` 关标签、
  `Ctrl+Shift+F` SFTP、`Ctrl+Shift+P` 端口转发、`Ctrl+Shift+T` 本地终端、
  `Ctrl+Shift+E` 左右分屏、`Ctrl+Shift+O` 上下分屏、`Ctrl+Shift+W` 切换分屏焦点、
  `Ctrl+Shift+C/V` 复制粘贴、`Ctrl+ +/-/0` 字体缩放、`Ctrl+鼠标滚轮` 缩放

命令行参数：`--host/--port/--username/--password`、`--no-autoconnect`、`--local`（附带打开本地终端）。

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
  hostio.py                 主机导入导出（OpenSSH config / JSON）
  sshclient.py              paramiko 封装：socket+Transport 自管、认证、指纹校验、SFTP
  session.py                SSHSession（QThread）：连接、读取、断开
  localsession.py           本地终端会话（ConPTY / pywinpty）
  sftpcore.py               SFTP 操作（列目录/收发/增删改，不依赖 Qt）
  sftpworker.py             SFTP 后台线程（任务队列）
  forwarding.py             三类端口转发（-L / -R / SOCKS5 -D）
  terminal/
    core.py                 pyte 封装 + 键盘序列编码
    view.py                 终端控件：网格渲染、选区、滚动、快捷键
  ui/
    main_window.py          主窗口：主机侧边栏 + 多标签（终端 / 分屏 / SFTP / 本地终端）
    host_dialog.py          新建/编辑主机（含「信任并记住指纹」）
    session_tab.py          会话标签（TerminalTab / SessionTab / LocalTerminalTab）
    split_tab.py            分屏容器
    sftp_panel.py           SFTP 双栏面板
    forward_dialog.py       端口转发管理对话框（三种类型）
tests/
  test_terminal.py          终端仿真核心单测
  test_terminal_view.py     滚动与排版回归测试
  test_storage.py           主机存储与凭据加密单测
  test_sftp_forward.py      SFTP 辅助函数与转发规则单测
  test_forwarding.py        桥接、SOCKS5、转发规则单测
  test_hostio.py            主机导入导出单测
  gui_smoke.py              离屏 GUI 冒烟（不需要联网）
  ssh_smoke.py              真实 SSH 冒烟（需要可达的服务器）
  gui_e2e.py                真实 GUI 端到端 + 截图
  gui_features_e2e.py       本地终端 / 分屏 / 三类转发 / 导入导出 GUI 验证
  sftp_forward_smoke.py     SFTP + 三种转发真实链路验证
  sftp_gui_e2e.py           SFTP 面板与转发对话框 GUI 验证
  key_auth_smoke.py         密钥认证验证（本地起 paramiko 服务器，不出网）
  forward_smoke.py          三种转发真实链路验证（单独跑）
  standalone_remote_forward.py  仅依赖 paramiko 的 -R 最小复现
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

# 新功能 GUI 验证：本地终端 / 分屏 / 三类转发 / 导入导出
.venv\Scripts\python.exe tests\gui_features_e2e.py --host 192.168.206.130 --password <密码>
```

## 五个已踩过的坑（改动时注意）

1. **必须使用 Fusion 样式**：Qt 6.11 在 Windows 上默认用 `windows11` 原生样式，
   该样式会忽略自定义 `QPainter` 的画笔颜色，终端文字会被画成系统默认文字色，
   在深色背景上几乎不可见（表现为"终端一片空白"）。所以在 `main.py` 里显式
   `app.setStyle("Fusion")`。
2. **回滚历史交给 pyte**：不要自己逐行比对去推断"滚了几行"。早期实现这样猜，
   每次输出都会误判并累加 `_offset`，最终把整个画面推出视口。现在直接用
   `HistoryScreen.history.top`，`_offset` 只由滚动条驱动。
3. **不要对 paramiko 的 `Channel` 用 `select()`**：Channel 的数据可能已经在其内部
   缓冲区里，此时 select 不一定报可读，表现为「单方向不通」（本机→远端能过、
   远端→本机丢数据）。统一用阻塞读 + 超时（`forwarding._pump`）。
4. **本地转发不能直接用 `request_port_forward`**：paramiko 的
   `Transport.request_port_forward()` 是**远端转发**（`ssh -R`）。`ssh -L` 必须
   本进程监听 socket，收到连接后再用 `direct-tcpip` 桥接。
5. **自己持有 socket + Transport，不要用 `SSHClient.connect()`**：paramiko 的
   `SSHClient.close()` 在传输层尚未创建时会直接返回，用户在「正在连接/认证」阶段
   关窗时线程会卡在 `Transport.start_client()`/认证等待里，进程退出阶段崩溃
   （实测 access violation，栈在 paramiko/packet.py 的 recv）。现在
   `SSHClient` 自己建 socket 与 Transport，任何阶段都能可靠打断；
   另外 `paramiko.PKey.from_path()` 传 str 类型 passphrase 会抛
   `TypeError: password must be bytes`，要用 `load_private_key()` 按类型加载。
