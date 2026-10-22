# Linux 部署指南

适用于已经配置好 EFB + 官方微信从端的 Linux 主机。管理器提供浏览器界面，不包含微信客户端，也不会自动安装、初始化 EFB 或填写 Telegram 配置。

## 1. 确认现有环境

- Python 3.9 或以上；已安装且能够正常运行 `ehforwarderbot`、`efb-wechat-slave` 和 `efb-telegram-master`。
- 使用原 EFB 的 Linux 用户和 Python 解释器。**虚拟环境不是必需条件**：原来是系统安装就继续使用原解释器，不需要迁移 EFB。
- 沿用原 EFB 的 HOME、profile 与环境变量；默认 profile 为 `default`。如果原服务配置过 `EFB_DATA_PATH`，应保留原值。
- 当前仅支持一个官方微信实例。管理器不能绕过微信网页版登录限制，也不能代替 EFB 本身的网络、代理和 Telegram 配置。

先确认 EFB 的启动脚本及其解释器：

```bash
command -v ehforwarderbot
head -n 1 "$(command -v ehforwarderbot)"
```

例如首行是 `#!/usr/bin/python3`，就使用 `/usr/bin/python3`；若是其他绝对路径，使用那个路径。如果首行通过 `/usr/bin/env python3` 启动，使用原 EFB 启动时的 PATH 解析出的 `python3`。如果原服务使用包装脚本，应核对原服务实际执行的 Python。以下以 `/usr/bin/python3` 为例，先检查环境：

```bash
/usr/bin/python3 --version
/usr/bin/python3 -c "import ehforwarderbot, efb_wechat_slave, efb_telegram_master, pyqrcode; print('EFB environment OK')"
```

原来使用虚拟环境的用户可先激活原环境，继续沿用其中的 Python。若原 Python 低于 3.9，当前管理器不能直接使用该解释器；不要只换一套找不到原 EFB 的 Python 来启动。

如果尚未安装 EFB，请先参考 [EFB 项目](https://github.com/ehForwarderBot/ehForwarderBot)、[Telegram 主端](https://github.com/ehForwarderBot/efb-telegram-master)和[官方微信从端](https://github.com/ehForwarderBot/efb-wechat-slave)完成安装及配置，确认原命令行链路可用后再接入管理器。

## 2. 停止原实例，安装管理器

先记录现有 EFB 版本、备份现有 profile，并使用原有方式停止 EFB（如果由 systemd 或其他守护程序管理，也应暂停原服务，避免自动拉起）。管理器只控制自己创建的子进程，无法接管已经在运行的 EFB。

### 系统安装的 EFB：直接运行源码

```bash
git clone https://github.com/chern91552/efb-manager.git
cd efb-manager
/usr/bin/python3 -m efb_manager.server --profile default --port 8765
```

将 `/usr/bin/python3` 替换为上一节确认的实际解释器。必须在克隆得到的项目目录运行，并保留该目录；管理器及其 EFB 子进程会从这里加载代码。此方法不执行 `pip install`，不修改系统 Python 的安装包。

### 原 EFB 已使用虚拟环境：可选安装方式

也可以在原虚拟环境里运行上述源码命令，或安装为独立命令：

```bash
source /path/to/efb-venv/bin/activate
git clone https://github.com/chern91552/efb-manager.git
cd efb-manager
python -m pip install .
efb-manager --profile default --port 8765
```

管理器只声明自身需要的 PyQRCode 依赖，不会主动安装 EFB。不要为了安装管理器直接升级现有 EFB 的全部依赖，也无需把原先系统安装的 EFB 搬进新虚拟环境。

## 3. 前台启动与扫码

源码运行时，在项目目录执行（如果上一节已启动，不要重复启动）：

```bash
/usr/bin/python3 -m efb_manager.server --profile default --port 8765
```

如果使用了安装方式，则运行原环境中的 `efb-manager --profile default --port 8765`。

保持该终端运行。在同一 Linux 用户的另一个终端读取管理口令：

```bash
cat ~/.local/state/efb-manager/access-token
```

浏览器打开 [http://127.0.0.1:8765](http://127.0.0.1:8765)，输入口令。进入“微信管理”，依次点击“启动 EFB”和“登录微信”，用手机微信扫描弹窗中的二维码，并在手机上确认。

首次启动从端时，EFB 可能在等待登录完成后才继续初始化其余组件。点击“退出微信”只结束 EFB 的微信会话，手机微信继续正常使用。主动退出后保持离线；再次登录需要手动点击。

管理器退出时会停止自己启动的 EFB。使用 `Ctrl+C` 停止前台管理器后，再按下一节启用后台服务。

### 远程服务器或 NAS

管理器固定监听 Linux 主机的 `127.0.0.1`，直接访问 `http://服务器IP:8765` 不可用。在你自己的电脑上执行：

```bash
ssh -N -L 8765:127.0.0.1:8765 YOUR_USER@YOUR_SERVER
```

保持 SSH 会话连接，然后在电脑浏览器打开 [http://127.0.0.1:8765](http://127.0.0.1:8765)。`YOUR_USER`、`YOUR_SERVER` 分别替换为服务器的 SSH 用户和地址。口令仍在服务器端读取。不要把访问口令放在网址里。

当前版本不提供公网监听或反向代理配置，不需要开放 8765 公网端口。

## 4. systemd 后台运行和开机启动

源码运行时，从项目目录复制专用模板，再编辑实际配置：

```bash
sudo cp deploy/efb-manager-source.service.example /etc/systemd/system/efb-manager.service
sudoedit /etc/systemd/system/efb-manager.service
```

必须替换的字段：

| 字段 | 填写方法 |
| --- | --- |
| `User=YOUR_USER` | 原 EFB 使用的 Linux 用户 |
| `WorkingDirectory=/home/YOUR_USER/efb-manager` | 实际克隆源码的绝对路径，源码模式必须从这里加载模块 |
| `ExecStart=/usr/bin/python3 -m efb_manager.server ...` | 原 EFB 使用的 Python 解释器绝对路径；系统安装或虚拟环境均可 |
| `--profile default` | 沿用原 EFB profile；有自定义名称时修改 |

若通过 pip 安装了管理器，也可以使用 `efb-manager.service.example` 模板，其 `ExecStart` 指向已安装的管理器命令。systemd 不会执行 shell 的 `source activate`，因此 `ExecStart` 必须写完整路径。若原 EFB 服务有代理、`EFB_DATA_PATH` 等环境变量，请在 `[Service]` 中用 `Environment=` 按原值配置；模板内有注释示例。

确认前台管理器与原 EFB 服务均已停止，然后执行：

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now efb-manager
sudo systemctl status efb-manager --no-pager
sudo journalctl -u efb-manager -n 50 --no-pager
```

此服务只自动启动管理器，EFB 和微信登录仍由页面手动启动。访问口令保存在服务用户 HOME 下的 `.local/state/efb-manager/access-token`，不是当前管理员或 root 的 HOME。

日常管理：

```bash
sudo systemctl restart efb-manager
sudo systemctl stop efb-manager
sudo journalctl -u efb-manager -f
```

重启管理器也会停止其 EFB 子进程；重启后需在页面重新启动 EFB 和登录微信。

## 5. 配置参数与数据

```bash
/usr/bin/python3 -m efb_manager.server --help
/usr/bin/python3 -m efb_manager.server --profile default --port 8765 --state-dir /your/private/efb-manager-state
```

| 参数 / 文件 | 说明 |
| --- | --- |
| `--profile` | EFB profile 名称，默认 `default` |
| `--port` | 本机监听端口，默认 `8765`；修改后 SSH 转发两端及浏览器地址应保持一致 |
| `--state-dir` | 管理器数据目录，默认 `~/.local/state/efb-manager`，应由运行用户独占访问 |
| `access-token` | 首次启动生成的管理口令 |
| `messages.sqlite3` 及同名 WAL/SHM 文件 | 文字消息和非文字占位符归档 |

管理器数据目录与 EFB profile 目录不同，需要分别备份。停止管理器后备份整个 state 目录，并另外备份原 EFB 的 profile。不要只复制运行中的 SQLite 主文件。

消息库跨服务重启保留，登录计时与上次在线时长目前不跨管理器重启保存。换微信账号时，如需隔离历史，请使用不同 EFB profile。该版本不提供数据自动清理和完整历史导入。

## 6. 更新与回退

先停止管理器并备份数据。从原项目目录运行：

```bash
git rev-parse HEAD
git pull --ff-only
sudo systemctl restart efb-manager
```

以上适用于源码运行；如果采用 pip 安装方式，更新源码后还需要用原解释器执行 `python -m pip install .` 再重启。

更新前记录旧 commit ID。需要回退时，在已停止管理器的情况下切回记录的提交，源码模式直接重启，安装模式使用原 Python 重新安装后启动服务。不要删除 state 和 EFB profile；代码回退不代表数据库结构一定可以回退。

## 7. 常见问题

| 现象 | 检查方法 |
| --- | --- |
| 找不到 `efb-manager` | 源码模式无需此命令，在项目目录使用原 Python 执行 `-m efb_manager.server`；安装模式检查命令绝对路径 |
| 找不到 EFB 或官方微信从端 | 用同一环境执行 `python -m pip show ...`，确认管理器和 EFB 安装位置一致 |
| 找不到原配置、提示未启用通道 | 核对服务用户、HOME、`EFB_DATA_PATH` 和 `--profile` 与原 EFB 一致 |
| 服务器 IP 无法打开页面 | 仅监听回环地址，请使用 SSH 转发；不要修改成公网绑定 |
| 口令无效 | 确认读取的是实际服务用户和实际 `--state-dir` 下的口令文件 |
| 端口已被占用 | 停止同机重复管理器，或选择其他端口并同步修改 SSH 转发及访问地址 |
| 二维码未出现 / 登录失败 | 先查看“运行日志”，确认 EFB 已启动、官方微信从端已配置，以及该账号可使用微信网页版 |
| 没有旧消息 | 只归档接入后经过 EFB 的消息，不导入手机微信已有历史 |
| 停止 EFB 后仍有历史记录 | 这是正常行为，记录在独立消息数据库内 |

## 验证范围

当前已通过本地模拟从端、HTTP 接口和浏览器验证，尚未完成真实 Linux 微信账号扫码及微信 / Telegram 实机收发验收。详细测试范围见 [VERIFICATION.md](../VERIFICATION.md)。
