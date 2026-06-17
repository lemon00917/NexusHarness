# NexusHarness 部署指南

## 环境说明

- **外网打包机**：Windows（当前开发机，可联网，需安装 Docker Desktop）
- **内网部署机**：Linux 服务器（完全离线，无外网，已安装 Docker）
- **Ollama**：已在 Linux 上独立部署

---

## 一、准备工作（Windows）

### 1. 预下载 pkuseg 源码

Docker 内网速极慢（~17KB/s），`pkuseg` 的 48.8MB 源码包需提前下载：

```powershell
cd D:\work\develop\AI\NexusHarness
curl -L -o pkuseg-0.0.25.tar.gz https://files.pythonhosted.org/packages/source/p/pkuseg/pkuseg-0.0.25.tar.gz
```

> 此文件已加入 `.dockerignore` 例外，会被 COPY 进镜像后从本地编译。

### 2. 构建 Docker 镜像

```powershell
cd D:\work\develop\AI\NexusHarness
docker build -t nexusharness:latest .
```

> **已知问题与处理**：
> - `python:3.12-slim` 不带 gcc/g++，改用 `python:3.12` 完整镜像
> - Docker 网络 HTTP 80 端口被封，apt-get 不可用，完整镜像自带编译工具绕过
> - pkuseg 的 Cython `.cpp` 引用 `longintrepr.h`（Python 3.12 已删除），Dockerfile 内用新版 Cython 重新生成
> - pkuseg 的 `setup.py` 依赖 numpy 但未在 `pyproject.toml` 声明，需手动先装 numpy
> - `sentence-transformers` 含 ~700MB PyTorch，已排除（默认走 Ollama embedding，不需要）
> - `html-to-markdown` 不在 requirements.txt，Dockerfile 单独安装

### 3. 导出镜像

```powershell
mkdir D:\work\develop\AI\nexus-deploy -Force
docker save -o D:\work\develop\AI\nexus-deploy\nexusharness.tar nexusharness:latest
# 可选：用 7-Zip 压缩为 .tar.gz，体积减半
```

---

## 二、内网 Linux 部署

### 1. 传输文件

将 `nexusharness.tar`（及压缩包）通过 U 盘 / 内网共享传到 Linux 服务器。

### 2. 加载镜像

```bash
# 压缩包先解压
gunzip nexusharness.tar.gz

docker load -i nexusharness.tar
docker images | grep nexusharness
```

### 3. 创建数据目录

```bash
mkdir -p /imedical/cdr/ai/nexus/{data,configs,logs,sessions,conversations,cache,results,web/rag_index,data/patients}
```

### 4. 启动容器

```bash
docker run -d \
  --name nexusharness \
  --network host \
  --restart unless-stopped \
  -e OLLAMA_HOST=http://localhost:11434 \
  -v /imedical/cdr/ai/nexus/data:/app/data \
  -v /imedical/cdr/ai/nexus/configs:/app/configs \
  -v /imedical/cdr/ai/nexus/logs:/app/logs \
  -v /imedical/cdr/ai/nexus/sessions:/app/sessions \
  -v /imedical/cdr/ai/nexus/conversations:/app/conversations \
  -v /imedical/cdr/ai/nexus/cache:/app/cache \
  -v /imedical/cdr/ai/nexus/results:/app/results \
  -v /imedical/cdr/ai/nexus/web/rag_index:/app/web/rag_index \
  nexusharness:latest
```

### 5. 验证

```bash
docker logs -f nexusharness
curl http://localhost:8000/api/rag/config
curl http://localhost:11434/api/tags
```

---

## 三、常见问题

### 1. ChromaDB 向量维度不匹配

```
Collection expecting embedding with dimension of 1024, got 2560
```

**原因**：旧索引用 1024 维模型建的，新模型（qwen3-embedding:4b）输出 2560 维。

```bash
# 清除旧索引，重新导入文档
docker stop nexusharness
rm -rf /imedical/cdr/ai/nexus/web/rag_index/chroma_db/*
docker start nexusharness
```

### 2. BM25 除零错误

```
ZeroDivisionError: float division by zero (rag.py:491)
```

已修复：BM25 全零匹配时 `max_bm25` 默认设为 1.0，避免除零。

### 3. Ollama 上下文超限

```
request (5225 tokens) exceeds the available context size (4096 tokens)
```

**原因**：`qwen2:7b-instruct` 默认上下文窗口仅 4096。

```bash
# 导出 Modelfile 并增大上下文
ollama show qwen2:7b-instruct --modelfile > /tmp/Modelfile
echo 'PARAMETER num_ctx 32768' >> /tmp/Modelfile
ollama create qwen2:7b-instruct -f /tmp/Modelfile

docker restart nexusharness
```

### 4. 权限错误

```
PermissionError: [Errno 13] Permission denied: '/app/logs/web.log'
```

已修复：去掉了 Dockerfile 中的 `USER appuser`，容器以 root 运行，挂载目录无权限问题。

---

## 四、新增 Ollama 模型

内网 Ollama 无法直接 `ollama pull`，需从外网下载后传输。

### 1. Windows 上拉取

```powershell
ollama pull qwen2.5:3b

# 查看模型文件
ollama show qwen2.5:3b --modelfile > D:\work\develop\AI\nexus-deploy\qwen2.5-3b.Modelfile
```

### 2. 导出模型文件

Ollama 数据目录（Windows）：`%USERPROFILE%\.ollama\`

```
.ollama/
├── models/
│   └── blobs/          # 模型权重（sha256 命名）
├── manifests/
│   └── registry.ollama.ai/
│       └── library/
│           └── qwen2.5/
│               └── 3b   # manifest 文件
```

把对应模型的 blob 和 manifest 复制到 Linux 相同路径下。

### 3. Linux 上导入

```bash
# 放到 Ollama 数据目录（通常 /usr/share/ollama/.ollama/ 或 ~/.ollama/）
# 验证
ollama list
```

---

## 五、常用操作

```bash
# 查看日志
docker logs -f nexusharness

# 重启
docker restart nexusharness

# 停止
docker stop nexusharness

# 升级镜像
docker stop nexusharness && docker rm nexusharness
docker rmi nexusharness:latest
docker load -i nexusharness-v2.tar
# 然后重新 docker run ...
```

---

## 六、架构总结

```
┌──────────────────────────────────────────────────────────┐
│  Linux 宿主机                                            │
│                                                          │
│  ┌──────────────────────────────────────────────────┐   │
│  │  Ollama（独立部署，宿主机进程）                     │   │
│  │  - qwen2.5:7b-instruct（num_ctx=32768）           │   │
│  │  - qwen2.5:3b                                     │   │
│  │  - qwen3-embedding:4b                             │   │
│  │  localhost:11434                                   │   │
│  └──────────────────────────────────────────────────┘   │
│                         ↑                                │
│                         │ --network host                 │
│                         ↓                                │
│  ┌──────────────────────────────────────────────────┐   │
│  │  NexusHarness 容器                                │   │
│  │  - Python 3.12 + 所有依赖（已内置）               │   │
│  │  - FastAPI :8000                                  │   │
│  │  - Volume 挂载：data/ configs/ logs/ ...          │   │
│  └──────────────────────────────────────────────────┘   │
└──────────────────────────────────────────────────────────┘
```

---

## 七、文件清单（部署包）

```
nexusharness.tar       # Docker 镜像
*.Modelfile            # 新增模型的 Modelfile（可选）
```
