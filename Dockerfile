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
# 运行阶段：uv Python 基础镜像 + 磁盘/BitLocker/SFTP 系统工具
############################################################
FROM ghcr.io/astral-sh/uv:python3.13-bookworm-slim AS runtime

# mount/dislocker-fuse/sshd 等特权操作要求 root（compose 以 privileged 运行）
USER root

# 系统工具说明：
#   dislocker      读取/解密旧版 BitLocker 加密卷（依赖 libfuse2）
#   cryptsetup     读取/解密新版 BitLocker 加密卷（内核 dm-crypt）
#   ntfs-3g        挂载解密出的 NTFS 卷 / 直接挂载 NTFS 分区
#   exfatprogs     exFAT 文件系统挂载辅助（mount.exfat）
#   openssh-server SFTP 服务（sshd + internal-sftp），把 /mnt/usb 共享给本机/局域网
#   util-linux     mount / umount / lsblk / blkid / losetup
#   udev           设备硬件数据库，lsblk/blkid 识别型号与文件系统
#   fuse3          FUSE 文件系统支持（dislocker/ntfs-3g 的挂载底座）
#   ca-certificates 证书校验
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        dislocker \
        cryptsetup \
        ntfs-3g \
        exfatprogs \
        openssh-server \
        util-linux \
        udev \
        fuse3 \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# mount/dislocker 等特权操作需要 root 运行（compose 使用 privileged）
WORKDIR /app

COPY --from=builder /app/.venv /app/.venv
COPY . .

# SFTP 共享根目录、sshd 特权分离目录与应用数据目录
RUN mkdir -p /mnt/usb /run/sshd /app/data/logs

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    TZ=Asia/Shanghai

EXPOSE 8000 22

# 健康检查（由 FastAPI 提供 /health）
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health').status==200 else 1)"

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
