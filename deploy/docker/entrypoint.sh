#!/bin/bash

set -e

cd /opt/doris-mcp-server/

# 前台运行
echo " ++++++ 启动 MCP Server ++++++ "
./start-mcp-server.sh

set +e
