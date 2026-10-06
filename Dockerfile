############################################################
# 构建阶段：使用 uv 安装 Python 依赖
############################################################
FROM ghcr.io/astral-sh/uv:python3.13-bookworm-slim AS builder

# 构建期配置：字节码编译、硬链接复制、依赖统一装到 /app/.venv
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/app/.venv

WORKDIR /app

# 先安装依赖（利用 BuildKit 缓存与绑定挂载，依赖不变时可命中缓存）
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    --mount=type=bind,source=README.md,target=README.md \
    uv sync --frozen --no-install-project --no-dev

# 再复制业务代码并安装项目本身
COPY . .
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev

############################################################
# 运行阶段：官方 Python slim + 磁盘/BitLocker/SFTP 系统工具
# （运行期不使用 uv：venv 自带 /usr/local/bin/python3 软链，
#  与官方 python:3.13-slim 的解释器路径一致）
############################################################
FROM python:3.13-slim-bookworm AS runtime

# mount/dislocker-fuse/sshd 等特权操作要求 root（compose 以 privileged 运行）
USER root

# 系统工具说明：
#   dislocker      读取/解密旧版 BitLocker 加密卷（依赖 libfuse2）
#   cryptsetup     读取/解密新版 BitLocker 加密卷（内核 dm-crypt）
#   ntfs-3g        挂载解密出的 NTFS 卷 / 直接挂载 NTFS 分区
#   exfatprogs     exFAT 文件系统挂载辅助（mount.exfat）
#   openssh-server SFTP 服务（sshd + internal-sftp），把 /mnt/usb 共享给本机/局域网
#   smartmontools  读取硬盘 SMART 健康度 / 温度（经 USB SAT 透传，不支持则自动隐藏）
#   util-linux     mount / umount / lsblk / blkid / losetup
#   udev           设备硬件数据库，lsblk/blkid 识别型号与文件系统
#   fuse3          FUSE 文件系统支持（dislocker/ntfs-3g 的挂载底座）
#   sshfs          远程存储：经 SFTP 协议把远程目录 FUSE 挂载为本地存储
#   tini           PID 1 init：收割 ntfs-3g/dislocker daemonize 后残留的僵尸进程
#   ca-certificates 证书校验
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
    dislocker \
    cryptsetup \
    ntfs-3g \
    exfatprogs \
    openssh-server \
    smartmontools \
    util-linux \
    udev \
    fuse3 \
    sshfs \
    tini \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# mount/dislocker 等特权操作需要 root 运行（compose 使用 privileged）
WORKDIR /app

COPY --from=builder /app/.venv /app/.venv
COPY . .

# SFTP 共享根目录与 BitLocker 解密中间层目录（/mnt/.vols，chroot 之外客户端不可见）、
# sshd 特权分离目录与应用数据目录
RUN mkdir -p /mnt/usb /mnt/.vols /run/sshd /app/data/logs

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    TZ=Asia/Shanghai

EXPOSE 8000 22

# 健康检查（由 FastAPI 提供 /health）
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health').status==200 else 1)"

# --proxy-headers + 信任全部来源的 X-Forwarded-*：容器内只可能由 compose
# 的 Caddy（tls profile）反代访问；直连 8000 本就是明文 HTTP，无信任边界问题
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips=*"]
