# ==============================================================================
# 多阶段构建：
#   Stage 1 (builder)  在容器内执行 ./build.sh，产出自包含发布包
#                      （server + client + docs + 内置 Python 运行时）
#   Stage 2 (runtime)  解压发布包并启动服务
#
# 目标平台由 BuildKit 自动注入的 TARGETARCH 决定：
#   linux/amd64  ->  ./build.sh linux-x64
#   linux/arm64  ->  ./build.sh linux-arm64
#
# 构建示例：
#   docker build -f deploy/docker/Dockerfile -t doris-mcp-server:dev .
#   docker buildx build --platform linux/amd64,linux/arm64 \
#       -f deploy/docker/Dockerfile -t doris-mcp-server:dev --push .
#
# 受限网络逃生通道（均可省略）：
#   --build-arg PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple
#       pip 走国内镜像装依赖（GitHub/PyPI 被墙时必配）
#   --build-arg USE_SYSTEM_PYTHON=0
#       默认 1：复用基础镜像自带的 Python 3.10.16（版本与 build.sh 要求一致），
#       完全跳过 GitHub 下载 python-build-standalone。设 0 才会走 GitHub 下载
#       （仅交叉构建 arm64 时必须走此路径，且需要能访问 GitHub）。
#   --build-arg DORIS_MCP_PYTHON_TARBALL=/path/to/cpython-*.tar.gz
#       使用预先下载好的 Python 独立包（需先放进构建上下文）
# ==============================================================================

######################################################################
# Stage 1: builder - 执行 build.sh 编译发布包
######################################################################
FROM docker.1ms.run/library/python:3.10.16 AS builder

# BuildKit 自动注入：amd64 / arm64
ARG TARGETARCH

# 可选构建参数（见文件头说明）
ARG PIP_INDEX_URL=""
# 默认复用基础镜像 Python，避免受限网络下 GitHub 下载失败
ARG USE_SYSTEM_PYTHON=1
ARG DORIS_MCP_PYTHON_TARBALL=""

WORKDIR /build

# 源码树（.dockerignore 已排除 dist/、python/、.git 等本地产物）
COPY . .

RUN set -eux; \
    chmod +x build.sh; \
    # 逃生通道映射到 build.sh 认识的环境变量
    if [ -n "$PIP_INDEX_URL" ]; then export PIP_INDEX_URL; fi; \
    # 复用基础镜像自带的 Python 3.10.16（与 build.sh 要求版本一致）。
    # 注意：build.sh 对交叉构建（TARGETARCH != 宿主架构）会忽略此变量，
    # 自动回退到 GitHub 下载 python-build-standalone。
    if [ "$USE_SYSTEM_PYTHON" = "1" ]; then \
        export DORIS_MCP_SYSTEM_PYTHON="$(command -v python3)"; \
    fi; \
    if [ -n "$DORIS_MCP_PYTHON_TARBALL" ]; then \
        export DORIS_MCP_PYTHON_TARBALL; \
    fi; \
    # TARGETARCH 兜底（legacy builder 不注入时按宿主架构判断）
    if [ -z "$TARGETARCH" ]; then \
        case "$(uname -m)" in \
            x86_64)  TARGETARCH=amd64 ;; \
            aarch64) TARGETARCH=arm64 ;; \
            *) echo "unsupported arch: $(uname -m)" >&2; exit 1 ;; \
        esac; \
    fi; \
    # 平台 -> build.sh 目标
    case "$TARGETARCH" in \
        amd64) ./build.sh linux-x64 ;; \
        arm64) ./build.sh linux-arm64 ;; \
        *) echo "unsupported TARGETARCH: $TARGETARCH" >&2; exit 1 ;; \
    esac; \
    ls -lh dist/

######################################################################
# Stage 2: runtime - 解压发布包并运行
# （发布包自带 Python 运行时，基础镜像只提供 OS 层依赖）
######################################################################
FROM docker.1ms.run/library/python:3.10.16

# 解压 Stage 1 产物（tar 包顶层目录固定为 doris-mcp-server/，与版本号无关）
COPY --from=builder /build/dist/*.tar.gz /tmp/
RUN set -eux; \
    tar -xzf /tmp/*.tar.gz -C /opt; \
    rm -f /tmp/*.tar.gz; \
    test -x /opt/doris-mcp-server/python/bin/python3

# 配置：仓库根目录的 mcp-server.toml 覆盖包内默认配置
COPY ./mcp-server.toml /opt/doris-mcp-server/

# 设置工作目录
WORKDIR /opt/doris-mcp-server/

# 设置入口点
COPY ./deploy/docker/entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh
CMD ["/usr/local/bin/entrypoint.sh"]
