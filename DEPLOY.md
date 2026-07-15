# NexusHarness 简单打包部署

整个更新流程只有三步：

```text
Windows 打包 -> 上传 nexusharness.tar -> Linux 重新创建容器
```

## 1. Windows 打包

在项目根目录打开 PowerShell：

```powershell
cd D:\work\develop\AI\NexusHarness

docker build --no-cache -t nexusharness:latest .
docker save -o nexusharness.tar nexusharness:latest
```

生成文件：

```text
D:\work\develop\AI\NexusHarness\nexusharness.tar
```

## 2. 上传到 Linux

使用 WinSCP 将 `nexusharness.tar` 上传到：

```text
/imedical/cdr/ai/nexusharness.tar
```

也可以执行：

```powershell
scp .\nexusharness.tar root@服务器IP:/imedical/cdr/ai/
```

## 3. Linux 更新并启动

登录 Linux 服务器，一次执行下面整段命令：

```bash
cd /imedical/cdr/ai

docker load -i nexusharness.tar

mkdir -p \
  /imedical/cdr/ai/nexus/data \
  /imedical/cdr/ai/nexus/configs \
  /imedical/cdr/ai/nexus/logs \
  /imedical/cdr/ai/nexus/web/rag_index

docker rm -f nexusharness 2>/dev/null || true

docker run -d \
  --name nexusharness \
  --network host \
  --restart unless-stopped \
  -e OLLAMA_HOST=http://localhost:11434 \
  -e MEDICAL_QUERY_DEBUG=1 \
  -e MEDICAL_QUERY_MAX_CONCURRENCY=4 \
  -e MEDICAL_QUERY_MAX_QUEUE=20 \
  -e MEDICAL_QUERY_QUEUE_TIMEOUT_SECONDS=300 \
  -e PYTHONUNBUFFERED=1 \
  -e PYTHONIOENCODING=utf-8 \
  -v /imedical/cdr/ai/nexus/data:/app/data \
  -v /imedical/cdr/ai/nexus/configs:/app/configs \
  -v /imedical/cdr/ai/nexus/logs:/app/logs \
  -v /imedical/cdr/ai/nexus/web/rag_index:/app/web/rag_index \
  nexusharness:latest

docker logs -f --tail 200 nexusharness
```

看到服务正常启动后，按 `Ctrl+C` 退出日志查看，不会停止容器。

访问地址：

```text
http://服务器IP:8000/templates/medical_filter.html
```

## 以后更新

代码修改后，重复上面的三步即可：

```text
重新 docker build 和 docker save
覆盖上传 nexusharness.tar
重新执行 Linux 更新命令
```

不要只执行：

```bash
docker restart nexusharness
```

`docker restart` 仍然使用旧容器中的旧代码，必须先 `docker rm -f nexusharness`，再执行 `docker run`。

## 数据和配置

更新镜像不会删除以下宿主机目录：

```text
/imedical/cdr/ai/nexus/data
/imedical/cdr/ai/nexus/configs
/imedical/cdr/ai/nexus/logs
/imedical/cdr/ai/nexus/web/rag_index
```

外部服务配置：

```text
/imedical/cdr/ai/nexus/configs/external_services.json
```

病历元数据来源配置：

```text
/imedical/cdr/ai/nexus/configs/medical_catalog_source.json
```

正常更新时不要删除 `/imedical/cdr/ai/nexus`。

## 病历筛选并发

上面的启动命令默认配置为：

```text
同时执行病历筛选：4 个
最多排队：20 个
排队最长等待：300 秒
```

第 5 个请求开始进入队列，网页会显示前方排队数量。队列满时接口返回
`429`，排队超时时返回 `503`，不会继续向线程池无限堆积任务。

这三个值可以直接在 `docker run` 的环境变量中调整。当前容器使用一个
Uvicorn worker，因此队列在该容器内统一生效；不要擅自增加
`uvicorn --workers`，多 worker 会形成多个互相独立的进程内队列。
