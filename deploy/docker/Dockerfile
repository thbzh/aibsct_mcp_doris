ARG REGISTRY_URL=gz01-srdart.srdcloud.cn/wx-wl-api/wx_wl_api-release-docker-local
ARG IMAGE_SUPERSET_BASE_VERSION=latest
######################################################################
# Node stage to deal with static asset construction
######################################################################
FROM docker.1ms.run/library/python:3.10.16

# 解压包（ADD 会自动解压 tar.gz）
ADD ./deploy/doris-mcp-server-0.2.11-linux_x64.tar.gz /opt/

# 配置
COPY ./mcp-server.toml /opt/doris-mcp-server/

# 设置工作目录
WORKDIR /opt/doris-mcp-server/

# 设置入口点
COPY ./deploy/docker/entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh
CMD ["/usr/local/bin/entrypoint.sh"]



