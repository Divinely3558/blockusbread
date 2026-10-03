# BlockUSBRead

在 Docker 容器中解密并挂载 BitLocker USB 硬盘，通过网页管理、用 **SMB 共享**读取文件。

单容器内置 FastAPI 管理后端与 Samba（smbd）：网页负责解锁 / 挂载 / 安全弹出，挂载后的卷通过 SMB 共享给局域网内的电脑、手机播放器直接访问，可流畅播放视频等大文件。

- **BitLocker**：支持用户密码与 48 位恢复密钥；dislocker 引擎优先，遇到新版元数据格式自动切换 cryptsetup
- **普通分区**：NTFS / exFAT / FAT32 / ext4
- **默认只读**，更安全；需要写入时可勾选读写模式（二次确认）
- **热插拔**：插入自动识别、拔出自动清理，网页实时刷新（SSE），无变化时不刷新
- **记住此卷**（需配置 `SECRET_KEY`）：凭据加密后存入本地，插入 / 重启后自动解锁
- **单端口管理 + SMB 共享**：管理页 `8000`，SMB 默认 `445`（可改）
- 移动友好的响应式网页界面

> 当前版本 **v0.0.2**。不支持 TPM 解锁、空闲自动锁定、TLS 与多用户。

---

## 目录

- [BlockUSBRead](#blockusbread)
  - [目录](#目录)
  - [环境要求](#环境要求)
  - [快速开始](#快速开始)
  - [环境变量](#环境变量)
  - [使用流程](#使用流程)
  - [访问 SMB 共享](#访问-smb-共享)
  - [常见问题](#常见问题)
  - [目录结构](#目录结构)
  - [版本记录](#版本记录)
    - [v0.0.1 — 2026-10-03](#v001--2026-10-03)

---

## 环境要求

- 已安装 Docker 与 Docker Compose（`docker compose` 插件）的 Linux 主机
- 容器以 `privileged` 运行并挂载 `/dev`，用于读取块设备、执行 mount 与 FUSE 挂载

## 快速开始

```bash
# 1. 进入项目目录
cd blockusbread

# 2. 创建环境配置文件
cp .env.example .env
# 编辑 .env：
#   ADMIN_PASSWORD=你的强密码
#   SECRET_KEY=$(openssl rand -base64 32)   # “记住此卷”加密密钥，必填

# 3. 构建并后台启动
docker compose up -d --build
```

启动后浏览器打开 `http://<宿主机IP>:8000`，用 `.env` 中的 `ADMIN_USER`（默认 `admin`）与 `ADMIN_PASSWORD` 登录。

常用命令：

```bash
docker compose logs -f          # 查看实时日志
docker compose restart          # 重启
docker compose down             # 停止（停止时自动卸载全部卷）
```

## 环境变量

在 `.env` 中配置：

| 变量             | 必填   | 默认    | 说明                                                                                                                          |
| ---------------- | ------ | ------- | ----------------------------------------------------------------------------------------------------------------------------- |
| `ADMIN_USER`     | 否     | `admin` | 管理员账号，网页登录与 SMB 共享共用                                                                                           |
| `ADMIN_PASSWORD` | **是** | —       | 管理员密码，网页与 SMB 共用。修改后执行 `docker compose up -d` 重建容器即生效                                                 |
| `SECRET_KEY` | **是** | — | “记住此卷”凭据的加密密钥，`openssl rand -base64 32` 生成。**一旦使用请勿更换**，否则已保存凭据无法解密 |
| `LOG_LEVEL`      | 否     | `INFO`  | 日志级别，`INFO` / `DEBUG`                                                                                                    |
| `SMB_HOST_PORT`  | 否     | `445`   | SMB 映射到宿主机的端口。宿主机自带 SMB 占用 445 时可改为 `1445` 等                                                            |

> 账号密码以容器环境变量为唯一来源，不提供网页改密入口。改密码：编辑 `.env` 后 `docker compose up -d`。

## 使用流程

1. 将 USB 硬盘插入宿主机，网页磁盘列表会自动出现该盘（无需刷新）。
2. BitLocker 卷：选择「用户密码」或「恢复密钥」，输入后点「解锁并挂载」。
   - 默认只读；勾选「以读写模式挂载」会弹出二次确认。
   - 勾选「记住此卷」（需配置 `SECRET_KEY`）后，以后插入 / 重启容器会自动解锁挂载。
3. 普通分区（NTFS / exFAT / FAT32 / ext4）可直接挂载。
4. 挂载成功后，页面显示该卷的 SMB 路径，通过 [SMB 共享](#访问-smb-共享) 访问文件。
5. 用完先在各客户端断开共享，再到网页点「安全弹出」；直接物理拔出也会自动清理挂载状态。

## 访问 SMB 共享

共享名固定为 `usb`，挂载后的卷在共享内路径为 `<磁盘ID>/part<序号>/fs`。
账号密码与网页登录相同，页面顶部信息条可一键复制地址、账号与密码（密码仅显示圆点）。

**Linux（本机或同网段）**

```bash
# 端口为 445 时省略 port= 参数
sudo mkdir -p /mnt/blockusb
sudo mount -t cifs //127.0.0.1/usb /mnt/blockusb \
  -o port=1445,username=admin,password=你的密码,iocharset=utf8,vers=3.0
# 用完：sudo umount /mnt/blockusb
```

图形文件管理器（Nautilus / Dolphin 等）地址栏：`smb://<宿主机IP>:1445/usb`

**macOS**：Finder → 前往 → 连接服务器，输入 `smb://<宿主机IP>:1445/usb`

**手机 / VLC 等播放器**：添加 SMB 主机 `<宿主机IP>`、端口 `1445`、共享 `usb`

**Windows**：资源管理器仅支持 445 端口，地址 `\\<宿主机IP>\usb`。若宿主机自身的 SMB 服务占用了 445，需停掉宿主 SMB 后保持默认端口映射，无法使用自定义端口。

## 常见问题

- **BitLocker 解锁失败？** 不支持 TPM / 智能卡解锁，仅支持密码与 48 位恢复密钥；新版 BitLocker 元数据会自动改用 cryptsetup 引擎，无需手动处理。
- **写入提示只读 / 受保护？** 默认以只读模式挂载。先安全弹出，再以读写模式重新解锁挂载。
- **SMB 连不上？** 确认 `SMB_HOST_PORT` 端口、防火墙放行；Linux 挂载需安装 `cifs-utils`。
- **中文文件名乱码？** 挂载时已统一使用 UTF-8；CIFS 挂载请带 `iocharset=utf8`。

## 目录结构

```
blockusbread/
├── app/
│   ├── devices/      # lsblk/blkid 设备扫描 + uevent 热插拔监听
│   ├── mounts/       # dislocker / cryptsetup / mount 编排与状态机
│   ├── samba/        # smbd 配置渲染、进程守护、登录校验
│   ├── web/          # FastAPI 路由、会话、SSE 事件
│   ├── static/       # 管理网页（HTML/CSS/JS + 图标）
│   └── ...           # 配置、日志、加密凭据存储、数据模型
├── docs/             # 需求文档
├── Dockerfile
├── docker-compose.yml
├── .env.example
└── VERSION
```

---

## 版本记录

按[语义化版本](https://semver.org/lang/zh-CN/)命名，时间倒序。

### v0.0.2 — 2026-10-03

**修复**

- 修复设备无变化时网页每隔数秒/十几秒整块重绘、持续闪烁的问题（定时扫描与无关内核事件不再触发推送，仅 USB 设备事件生效；数据无变化时前端不重绘，输入中的解锁密码也不再被清空）。
- 修复卷标 / 硬盘型号含 **GBK 编码中文**（Windows 中文环境常见）时 `lsblk` 输出无法按 UTF-8 解码、导致设备扫描整体失败、磁盘列表不出现的问题；现按 UTF-8 → GBK → 替换非法字符三级兼容解码，中文卷标可正常显示。

### v0.0.1 — 2026-10-03

**新增**

- 首个可用版本：单容器内置 FastAPI 管理后端与 Samba，提供网页管理（端口 8000）与 SMB 文件共享（端口 445，可经 `SMB_HOST_PORT` 修改）。
- BitLocker 卷支持**用户密码**与 **48 位恢复密钥**解锁；dislocker 引擎优先，遇到新版元数据格式自动切换 cryptsetup。
- 支持 NTFS / exFAT / FAT32 / ext4 普通分区直接挂载，中文文件名统一 UTF-8。
- 挂载默认**只读**；可勾选读写模式并需二次确认。
- USB 硬盘热插拔自动识别（uevent + SSE 实时刷新），拔出自动清理，支持网页「安全弹出」。
- 可选「记住此卷」：凭据用 `SECRET_KEY` 加密存入本地 SQLite，插入 / 重启后自动解锁。
- 账号密码由 `ADMIN_USER` / `ADMIN_PASSWORD` 环境变量管理，网页登录与 SMB 共享共用一套；页面信息条可复制 SMB 地址、账号与密码（密码隐藏显示）。
- 响应式品牌绿管理界面，适配手机等窄屏设备。
