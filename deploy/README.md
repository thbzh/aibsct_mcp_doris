# 编译打包

```shell

```

## build 镜像

```shell
# 多架构构建
export REGISTRY_URL=localhost:5000
export REGISTRY_URL=gz01-srdart.srdcloud.cn/wx-wl-api/wx_wl_api-release-docker-local     # 研发云（测试）
cd deploy/docker/
docker buildx bake --allow=fs.read=* -f build-compose.yml --push 
```
