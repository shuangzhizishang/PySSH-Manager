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
- **快捷键**：`Ctrl+N` 新建、`Ctrl+Enter` 连接、`Ctrl+R` 重连、`Ctrl+W` 关标签、
  `Ctrl+Shift+C/V` 复制粘贴、`Ctrl+ +/-/0` 字体缩放、`Ctrl+鼠标滚轮` 缩放

尚未实现（后续计划）：SFTP 文件浏览器、端口转发界面、会话分屏、主机导入导出、本地终端。

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
  sshclient.py              paramiko 封装：认证、指纹校验、PTY、SFTP、转发
  session.py                SSHSession（QThread）：连接、读取、断开
  terminal/
    core.py                 pyte 封装 + 键盘序列编码
    view.py                 终端控件：网格渲染、选区、滚动、快捷键
  ui/
    main_window.py          主窗口：主机侧边栏 + 多标签终端
    host_dialog.py          新建/编辑主机（含「信任并记住指纹」）
    session_tab.py          单个会话标签
tests/
  test_terminal.py          终端仿真核心单测
  test_terminal_view.py     滚动与排版回归测试
  test_storage.py           主机存储与凭据加密单测
  gui_smoke.py              离屏 GUI 冒烟（不需要联网）
  ssh_smoke.py              真实 SSH 冒烟（需要可达的服务器）
  gui_e2e.py                真实 GUI 端到端 + 截图
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
```

## 两个已踩过的坑（改动时注意）

1. **必须使用 Fusion 样式**：Qt 6.11 在 Windows 上默认用 `windows11` 原生样式，
   该样式会忽略自定义 `QPainter` 的画笔颜色，终端文字会被画成系统默认文字色，
   在深色背景上几乎不可见（表现为"终端一片空白"）。所以在 `main.py` 里显式
   `app.setStyle("Fusion")`。
2. **回滚历史交给 pyte**：不要自己逐行比对去推断"滚了几行"。早期实现这样猜，
   每次输出都会误判并累加 `_offset`，最终把整个画面推出视口。现在直接用
   `HistoryScreen.history.top`，`_offset` 只由滚动条驱动。
