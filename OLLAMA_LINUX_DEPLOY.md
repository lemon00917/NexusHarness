# Linux CPU 服务器部署 Ollama

本文用于在以下服务器上部署 Ollama，并与 NexusHarness 配套运行。全流程按内网、无公网访问设计。

| 项目 | 配置 |
| --- | --- |
| CPU | 16 核 |
| GPU | 无，全部使用 CPU 推理 |
| 内存 | 32 GB |
| 磁盘 | 机械硬盘，无 SSD |
| 网络 | 内网环境，目标服务器不能直接访问公网 |
| 推荐系统 | Ubuntu 22.04/24.04 LTS 或 Debian 12，x86_64 |

文档按 2026-08-11 的 Ollama 官方 Linux 文档和当前 NexusHarness 实现整理。默认 Ollama 与 NexusHarness 部署在同一台内网服务器，Ollama 只监听本机 `127.0.0.1:11434`。安装程序和模型必须先在外网中转机准备，再通过单位允许的文件交换方式传入内网。

## 1. 推荐部署方案

这台服务器可以运行 1.5B、3B 和 7B 量化模型，但没有 GPU，推理速度主要受 CPU、上下文长度和并发数影响；机械硬盘主要影响模型下载、首次加载和模型切换。

### 1.1 性能优先方案

全部业务阶段统一使用 `qwen2.5:3b`：

```text
router_model=qwen2.5:3b
judge_model=qwen2.5:3b
planner_model=qwen2.5:3b
```

对应 Ollama 配置：

```text
OLLAMA_NUM_PARALLEL=1
OLLAMA_MAX_LOADED_MODELS=1
OLLAMA_CONTEXT_LENGTH=4096
```

该方案只有一个常驻模型，最适合机械硬盘，冷启动和模型切换最少。

### 1.2 兼容现有业务方案

使用当前项目常见的两个模型：

```text
router_model=qwen2.5:3b
judge_model=qwen2.5:3b
planner_model=deepseek-r1:1.5b
```

对应 Ollama 配置：

```text
OLLAMA_NUM_PARALLEL=1
OLLAMA_MAX_LOADED_MODELS=2
OLLAMA_CONTEXT_LENGTH=4096
```

`qwen2.5:3b` 模型文件约 1.9 GB，`deepseek-r1:1.5b` 约 1.1 GB。32 GB 内存可以承载这两个模型和 4K 上下文，但仍需要给操作系统、NexusHarness 和文件缓存预留内存。

### 1.3 质量优先方案

需要更强判断能力时，可以把 `judge_model` 改为 `qwen2.5:7b`。该模型文件约 4.7 GB，但 CPU 推理延迟会明显增加，上线前必须使用真实请求压测。

不建议在该服务器上使用 14B 及以上模型，也不建议同时运行多个 7B 模型。

当前 NexusHarness 对名称包含 `reader-lm` 的模型会强制使用 `131072` 上下文。该配置不适合这台 32 GB、纯 CPU 的服务器，除非单独完成内存和延迟压测；常规部署不要在此机器上启用 `reader-lm`。

## 2. 容量和并发规划

推荐初始值：

| 项目 | 初始值 | 说明 |
| --- | --- | --- |
| Ollama 单模型并行数 | `1` | 防止 CPU 推理互相争抢 |
| Ollama 常驻模型数 | `1` 或 `2` | 按实际使用的模型数量设置 |
| Ollama 队列 | `8` | 队列满后尽快返回过载，不无限积压 |
| 上下文长度 | `4096` | 当前 NexusHarness 客户端默认也是 4096 |
| 病历筛选并发 | `1` | 稳定后最多先尝试 `2` |
| 病历筛选队列 | `10` | 防止大量请求堆积 |
| 模型盘剩余空间 | 至少 20 GB | 包含模型、更新和临时空间 |

Ollama 官方说明中，CPU 推理默认最多可同时加载 3 个模型；并行请求会按照 `OLLAMA_NUM_PARALLEL * OLLAMA_CONTEXT_LENGTH` 增加内存需求。本服务器应主动降低默认并发和常驻模型数。

机械硬盘环境尤其要避免频繁切换模型。当前 NexusHarness 的 Ollama 请求会传入 `keep_alive=-1`，因此模型会尽量常驻；如果业务同时使用两个模型，`OLLAMA_MAX_LOADED_MODELS` 应设为 `2`。如果统一使用单模型，则设为 `1`。

## 3. 上线前检查

使用具备 `sudo` 权限的账号执行：

```bash
uname -m
cat /etc/os-release
lscpu | egrep 'Architecture|CPU\(s\)|Model name|Thread|Core|Socket'
nproc
free -h
swapon --show
df -hT
df -i
```

确认：

1. CPU 架构为 `x86_64`。
2. 模型目录所在分区至少剩余 20 GB；后续增加模型时建议预留 30-50 GB。
3. 系统至少保留 6-8 GB 可用内存给操作系统和业务服务。
4. 不要把模型目录放在容器临时层、内存盘或会定期清理的目录。
5. 如果服务器已有 swap，确认当前没有持续使用大量 swap。机械硬盘上的 swap 只能防止 OOM，不能提供可接受的推理性能。

如果 openEuler 配置了内网软件源，可安装检查工具。不同系统的命令不同。不要把软件源改成公网源：

```bash
# Ubuntu/Debian
sudo apt-get update
sudo apt-get install -y ca-certificates jq procps psmisc lsof htop sysstat zstd
```

```bash
# openEuler/CentOS/RHEL/Rocky/Alma/Fedora
sudo dnf install -y ca-certificates jq procps-ng psmisc lsof htop sysstat zstd
# 老版本系统没有 dnf 时使用：
sudo yum install -y ca-certificates jq procps psmisc lsof htop sysstat zstd
```

```bash
# openSUSE
sudo zypper --non-interactive install ca-certificates jq procps psmisc lsof htop sysstat zstd
```

如果没有内网软件源，应由运维提前提供 `zstd`、`jq`、`sysstat` 等离线软件包及其依赖。`zstd` 用于解压 Ollama 和模型归档；`jq`、`htop`、`sysstat` 只用于检查和排障，不是 Ollama 启动的硬依赖。部署包中的 `install_ollama_offline.sh` 只会使用服务器已配置的软件源尝试安装 `zstd`，不会主动访问公网；如果服务器没有内网软件源，请先安装本地 RPM 包，再运行脚本。

## 4. 离线安装 Ollama

### 4.1 在外网中转机准备安装包

中转机应使用可信网络，并下载与目标服务器架构匹配的 Linux 安装包。本文目标是 `x86_64`。

Linux 中转机执行：

```bash
mkdir -p ollama-offline
cd ollama-offline
curl -fL https://ollama.com/download/ollama-linux-amd64.tar.zst \
  -o ollama-linux-amd64.tar.zst
sha256sum ollama-linux-amd64.tar.zst > ollama-linux-amd64.tar.zst.sha256
```

Windows 中转机可以通过浏览器下载安装包，然后在安装包所在目录打开 PowerShell，执行：

```powershell
$file = 'ollama-linux-amd64.tar.zst'
$hash = (Get-FileHash -LiteralPath $file -Algorithm SHA256).Hash.ToLowerInvariant()
Set-Content -LiteralPath ($file + '.sha256') -Value ($hash + '  ' + $file) -Encoding ascii
Get-Content -LiteralPath ($file + '.sha256')
```

该命令会生成 Linux `sha256sum -c` 可以直接读取的 `ollama-linux-amd64.tar.zst.sha256`。如果只需要在 Windows 上查看校验值，也可以在 CMD 中执行：

```bat
certutil -hashfile ollama-linux-amd64.tar.zst SHA256
```

`certutil` 输出不能直接作为 `sha256sum -c` 的校验文件；离线交付仍建议使用上一段 PowerShell 命令生成标准格式文件。

同时记录准备日期和 Ollama 版本。建议把以下内容一并交付内网：

```text
ollama-linux-amd64.tar.zst
ollama-linux-amd64.tar.zst.sha256
ollama-models.tar.zst
ollama-models.tar.zst.sha256
OFFLINE_MANIFEST.txt
```

`OFFLINE_MANIFEST.txt` 至少记录 Ollama 版本、模型标签、文件 SHA-256、准备时间和准备人。文件传入内网时应使用单位批准的介质、摆渡机或制品库，并完成病毒扫描和文件完整性校验。

### 4.2 在内网目标机校验并安装

假设离线文件已放到 `/opt/offline/ollama`：

```bash
cd /opt/offline/ollama
sha256sum -c ollama-linux-amd64.tar.zst.sha256
sudo tar --zstd -xf ollama-linux-amd64.tar.zst -C /usr
command -v ollama
ollama --version
```

如果系统的 `tar` 不支持 `--zstd`，先从内网软件源安装 `zstd`，或在外网机将归档转换为普通 `.tar` 后再传入。不要跳过 SHA-256 校验。

### 4.3 创建服务账号和 systemd 服务

官方安装脚本会自动创建服务；离线手动安装时需要自行完成：

```bash
getent group ollama >/dev/null || sudo groupadd --system ollama
id ollama >/dev/null 2>&1 || sudo useradd \
  --system --gid ollama --home-dir /usr/share/ollama \
  --create-home --shell /usr/sbin/nologin ollama

sudo tee /etc/systemd/system/ollama.service >/dev/null <<'EOF'
[Unit]
Description=Ollama Service
After=network-online.target

[Service]
ExecStart=/usr/bin/ollama serve
User=ollama
Group=ollama
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF
```

确认二进制位置。如果 `command -v ollama` 不是 `/usr/bin/ollama`，应把 `ExecStart` 改为实际绝对路径。

### 4.4 仅限可访问公网的环境

如果将来某台服务器经过审批可以访问公网，才可使用官方安装脚本：

```bash
curl -fsSL https://ollama.com/install.sh | sh
```

检查安装结果：

```bash
command -v ollama
ollama --version
sudo systemctl status ollama --no-pager
```

本项目的目标内网服务器不要执行上述联网命令。

## 5. 配置模型目录

以下示例将模型放在 `/data/ollama/models`。如果大容量机械盘挂载点不是 `/data`，应替换为实际路径。

```bash
sudo mkdir -p /data/ollama/models
sudo chown -R ollama:ollama /data/ollama
sudo chmod 750 /data/ollama /data/ollama/models
namei -l /data/ollama/models
```

如果 `/data` 不存在，先通过 `lsblk -f` 和 `findmnt` 确认实际数据盘挂载点，不要直接把目录建到空间不足的系统盘。

## 6. 配置 systemd

创建 Ollama 的 systemd 覆盖配置：

```bash
sudo mkdir -p /etc/systemd/system/ollama.service.d
sudo tee /etc/systemd/system/ollama.service.d/override.conf >/dev/null <<'EOF'
[Service]
Environment="OLLAMA_HOST=127.0.0.1:11434"
Environment="OLLAMA_MODELS=/data/ollama/models"
Environment="OLLAMA_CONTEXT_LENGTH=4096"
Environment="OLLAMA_NUM_PARALLEL=1"
Environment="OLLAMA_MAX_LOADED_MODELS=2"
Environment="OLLAMA_MAX_QUEUE=8"
Environment="OLLAMA_KEEP_ALIVE=30m"
EOF
```

如果采用“单模型性能优先方案”，把下面一项改成 `1`：

```ini
Environment="OLLAMA_MAX_LOADED_MODELS=1"
```

参数说明：

| 参数 | 作用 |
| --- | --- |
| `OLLAMA_HOST` | 只监听本机端口，避免无认证 API 暴露到网络 |
| `OLLAMA_MODELS` | 指定模型持久化目录 |
| `OLLAMA_CONTEXT_LENGTH` | 默认上下文长度；越大越耗内存且 CPU 预填充越慢 |
| `OLLAMA_NUM_PARALLEL` | 每个模型同时处理的请求数 |
| `OLLAMA_MAX_LOADED_MODELS` | 最多同时常驻的模型数 |
| `OLLAMA_MAX_QUEUE` | Ollama 忙时允许排队的请求数 |
| `OLLAMA_KEEP_ALIVE` | 普通客户端请求结束后默认保留模型的时间 |

注意：API 请求中的 `keep_alive` 会覆盖 `OLLAMA_KEEP_ALIVE`。当前 NexusHarness 客户端传入 `keep_alive=-1`，因此 NexusHarness 调用过的模型会保持加载，直到 Ollama 因内存或模型数量限制将其卸载。

应用配置：

```bash
sudo systemctl daemon-reload
sudo systemctl enable ollama
sudo systemctl restart ollama
sudo systemctl status ollama --no-pager
```

检查 systemd 实际加载的环境变量：

```bash
sudo systemctl show ollama --property=Environment --no-pager
```

检查监听端口：

```bash
ss -lntp | grep 11434
curl --fail http://127.0.0.1:11434/api/tags | jq
```

预期监听地址为 `127.0.0.1:11434`，不应是 `0.0.0.0:11434`。

## 7. 离线准备和导入模型

目标服务器不能直接执行 `ollama pull`。应在一台可访问 Ollama 模型库的 `x86_64` Linux 中转机上安装相同版本的 Ollama，下载模型后打包整个模型目录。不要只复制单个 blob；Ollama 还需要 manifests 等目录信息。

### 7.1 在外网中转机生成模型包

以下命令启动一个独立的临时 Ollama 实例，避免污染中转机原有模型目录：

```bash
mkdir -p $HOME/ollama-offline/bundle/models
cd $HOME/ollama-offline

OLLAMA_HOST=127.0.0.1:11435 \
OLLAMA_MODELS=$PWD/bundle/models \
nohup ollama serve > ollama-staging.log 2>&1 &
OLLAMA_STAGING_PID=$!

sleep 5
OLLAMA_HOST=127.0.0.1:11435 ollama pull qwen2.5:3b
OLLAMA_HOST=127.0.0.1:11435 ollama pull deepseek-r1:1.5b
OLLAMA_HOST=127.0.0.1:11435 ollama list

kill $OLLAMA_STAGING_PID
wait $OLLAMA_STAGING_PID 2>/dev/null || true

tar --zstd -C $PWD/bundle -cf ollama-models.tar.zst models
sha256sum ollama-models.tar.zst > ollama-models.tar.zst.sha256
du -h ollama-models.tar.zst
```

单模型性能优先方案只拉取 `qwen2.5:3b`。只有完成真实业务压测并接受更高延迟时，才额外拉取 `qwen2.5:7b`。打包前用 `ollama list` 核对模型标签，避免把无关模型带入内网。

如果中转机命令失败，先查看 `ollama-staging.log`，并确认 `11435` 端口没有被其他进程占用。中转机准备完成后，将模型包、校验文件和安装包一起按内网文件交换流程传输。

### 7.2 在内网目标机导入模型

```bash
cd /opt/offline/ollama
sha256sum -c ollama-models.tar.zst.sha256

sudo systemctl stop ollama
sudo tar --zstd -xf ollama-models.tar.zst -C /data/ollama
sudo chown -R ollama:ollama /data/ollama/models
sudo chmod -R u=rwX,g=rX,o= /data/ollama/models
sudo systemctl start ollama

ollama list
du -sh /data/ollama/models
```

导入更新包会与现有模型目录合并。上线包应包含业务配置中引用的全部模型标签；模型名称和标签必须完全一致，例如 `qwen2.5:3b` 不能写成 `qwen2.5`。

## 8. 基础功能验证

CLI 测试：

```bash
ollama run qwen2.5:3b '只输出：部署测试通过'
```

HTTP API 测试：

```bash
curl --fail http://127.0.0.1:11434/api/generate \
  -H 'Content-Type: application/json' \
  -d '{
    "model": "qwen2.5:3b",
    "prompt": "只输出：OK",
    "stream": false,
    "options": {"num_ctx": 4096, "num_predict": 32}
  }' | jq
```

检查当前加载模型和 CPU 推理状态：

```bash
ollama ps
curl --fail http://127.0.0.1:11434/api/ps | jq
```

无 GPU 服务器上，`ollama ps` 的 `PROCESSOR` 应显示 `100% CPU`。

## 9. 与 NexusHarness 对接

当前 NexusHarness 的 `OllamaClient` 默认连接：

```text
http://localhost:11434
```

因此，Ollama 和 NexusHarness 在同一台服务器时，业务容器必须使用 host 网络：

```bash
docker run -d \
  --name nexusharness \
  --network host \
  --restart unless-stopped \
  -e MEDICAL_QUERY_MAX_CONCURRENCY=1 \
  -e MEDICAL_QUERY_MAX_QUEUE=10 \
  -e MEDICAL_QUERY_QUEUE_TIMEOUT_SECONDS=300 \
  nexusharness:latest
```

当前代码中的 `OllamaClient` 默认地址仍是 `http://localhost:11434`，并且构造器目前没有读取容器环境变量 `OLLAMA_HOST`。因此，设置 `-e OLLAMA_HOST=...` 不能改变客户端连接地址，也不能替代 host 网络。没有使用 `--network host` 时，容器里的 `127.0.0.1` 指向容器自身，业务将连接不到宿主机 Ollama。

项目当前还使用一个进程级 Ollama 信号量，最多允许 2 个模型调用同时进入客户端。对本服务器仍应把 `MEDICAL_QUERY_MAX_CONCURRENCY` 从 `1` 起步，因为一次病历筛选可能包含多个 LLM 阶段，不能只依据该信号量估算业务并发能力。

上线初期保持：

```text
MEDICAL_QUERY_MAX_CONCURRENCY=1
OLLAMA_NUM_PARALLEL=1
```

稳定运行后，可以只把业务并发试调为 `2`；不要同时提高业务并发和 `OLLAMA_NUM_PARALLEL`。

## 10. 机械硬盘专项优化

1. 模型目录必须放本地磁盘，不要放 NFS、SMB 或其他网络文件系统。
2. 尽量固定使用一到两个模型，避免在多个模型之间频繁切换。
3. 保持模型分区至少 20% 空闲空间。
4. 不要定时清理模型缓存，也不要在业务高峰期执行模型更新。
5. 重启后第一次请求会加载模型，耗时明显高于后续请求，这是正常冷启动。
6. 如果只能使用一个常驻模型，应让路由、判断和规划统一使用 `qwen2.5:3b`。
7. 使用两个模型时设置 `OLLAMA_MAX_LOADED_MODELS=2`，利用 32 GB 内存减少机械盘重复读取。

查看磁盘负载：

```bash
iostat -xz 5 3
```

重点观察：

- `%util` 长期接近 `100%`：磁盘持续满载。
- `await` 持续很高：I/O 等待严重。
- 系统 load 很高但 CPU idle 仍较多：可能在等待磁盘。

出现上述情况时，先降低并发并减少模型切换，不要先增加请求队列。

## 11. 内存和 swap

检查内存：

```bash
free -h
vmstat 1 10
swapon --show
```

建议：

- 正常运行时至少保留 4-6 GB `available` 内存。
- 如果 `si/so` 持续非零，说明系统正在频繁换页，应降低并发或减少常驻模型。
- 机械硬盘上的 swap 会导致推理速度急剧下降，只能作为防止进程被 OOM Killer 终止的最后保护。
- 如果服务器完全没有 swap，可按运维规范配置 8 GB 紧急 swap，但不能依赖 swap 承载模型。

检查是否发生 OOM：

```bash
sudo journalctl -k --since '1 hour ago' | grep -i -E 'oom|out of memory|killed process'
```

## 12. 性能基线测试

使用非流式请求记录完整耗时：

```bash
curl --fail http://127.0.0.1:11434/api/generate \
  -H 'Content-Type: application/json' \
  -d '{
    "model": "qwen2.5:3b",
    "prompt": "用三句话说明肺炎的常见临床表现。",
    "stream": false,
    "options": {"num_ctx": 4096, "num_predict": 128}
  }' | jq '{
    total_duration,
    load_duration,
    prompt_eval_count,
    prompt_eval_duration,
    eval_count,
    eval_duration
  }'
```

Ollama 返回的时长单位为纳秒。重点记录：

- `load_duration`：模型加载耗时，机械硬盘冷启动时会较大。
- `prompt_eval_duration`：输入上下文处理耗时。
- `eval_duration / eval_count`：单 token 平均生成耗时。
- 第一次请求和第二次请求的差异：用于判断模型是否被重复加载。

至少分别测试：

1. 服务刚重启后的第一次请求。
2. 同模型第二次请求。
3. 两个模型交替请求。
4. 一个并发和两个并发。
5. NexusHarness 的真实病历筛选请求。

## 13. 日常运维

查看状态和日志：

```bash
sudo systemctl status ollama --no-pager
sudo journalctl -u ollama -n 200 --no-pager
sudo journalctl -u ollama -f
```

查看模型：

```bash
ollama list
ollama ps
```

停止不再需要的常驻模型：

```bash
ollama stop qwen2.5:7b
```

内网更新 Ollama：

```bash
cd /opt/offline/ollama
sha256sum -c ollama-linux-amd64.tar.zst.sha256
sudo systemctl stop ollama
sudo tar --zstd -xf ollama-linux-amd64.tar.zst -C /usr
sudo systemctl restart ollama
ollama --version
```

新安装包应先在测试环境验证，确认与现有模型兼容后再进入生产内网。更新后重新执行 `/api/tags`、最小推理和真实业务请求。不要删除 `/data/ollama/models`，否则需要重新导入全部模型。

## 14. 网络和安全

Ollama API 默认没有项目级业务认证。本机部署时保持：

```ini
Environment="OLLAMA_HOST=127.0.0.1:11434"
```

不要把 `11434` 开放到公网或整个办公网。当前项目建议 Ollama 与 NexusHarness 同机部署并保持回环地址监听。

如果以后必须分开部署，优先让 Ollama 只监听服务器的指定内网 IP，而不是 `0.0.0.0`，并且必须同时配置：

1. 内网防火墙或安全组只允许 NexusHarness 服务器固定 IP。
2. Linux 防火墙只允许该固定 IP。
3. 跨安全域时使用带认证和 TLS 的反向代理，不直接暴露 Ollama API。
4. 先修改 NexusHarness 的 Ollama base URL 配置；当前客户端默认使用 `localhost`，只改 Ollama 监听地址无效。

建议新增明确的客户端变量（例如 `OLLAMA_BASE_URL`），不要与 Ollama 服务端用于控制监听地址的 `OLLAMA_HOST` 混用。

## 15. 常见故障

### 15.1 服务启动失败

```bash
sudo systemctl status ollama --no-pager
sudo journalctl -u ollama -n 200 --no-pager
sudo systemctl cat ollama
```

检查 systemd 配置语法、二进制路径和模型目录权限。

### 15.2 `permission denied`

```bash
sudo chown -R ollama:ollama /data/ollama
sudo chmod 750 /data/ollama /data/ollama/models
namei -l /data/ollama/models
sudo systemctl restart ollama
```

### 15.3 离线模型导入失败

```bash
cd /opt/offline/ollama
sha256sum -c ollama-models.tar.zst.sha256
df -h /data/ollama/models
sudo journalctl -u ollama -n 200 --no-pager
find /data/ollama/models -maxdepth 2 -type d -print
ollama list
```

检查归档校验值、磁盘空间、目录层级、文件属主和 Ollama 版本。正确目录应包含 `/data/ollama/models/blobs` 和 `/data/ollama/models/manifests`。目标服务器是内网环境，不应通过配置公网代理来绕过离线交付流程。

### 15.4 推理很慢

依次检查：

1. `ollama ps` 是否频繁出现模型加载和卸载。
2. `free -h` 是否已经大量使用 swap。
3. `iostat -xz 5 3` 是否显示磁盘满载。
4. 是否把上下文从 4096 提高到了 8192 或更高。
5. 是否使用了 7B 模型。
6. Ollama 或业务并发是否大于 1。

### 15.5 NexusHarness 连接失败

先在宿主机测试：

```bash
curl --fail http://127.0.0.1:11434/api/tags
```

再进入容器测试：

```bash
docker exec nexusharness \
  python -c "import requests; print(requests.get('http://127.0.0.1:11434/api/tags', timeout=5).status_code)"
```

宿主机成功而容器失败时，优先检查容器是否使用 `--network host`。

### 15.6 Ollama 返回 503

表示请求队列已满或服务过载。检查：

```bash
sudo journalctl -u ollama -n 200 --no-pager
ollama ps
uptime
free -h
```

应降低业务并发，而不是继续增大 `OLLAMA_MAX_QUEUE`。

## 16. 上线验收清单

- [ ] `ollama --version` 正常返回。
- [ ] `systemctl is-enabled ollama` 返回 `enabled`。
- [ ] `systemctl is-active ollama` 返回 `active`。
- [ ] `ss -lntp` 显示只监听 `127.0.0.1:11434`。
- [ ] `/api/tags` 返回业务需要的模型。
- [ ] `ollama ps` 显示 `100% CPU`。
- [ ] 模型实际存放在 `/data/ollama/models`。
- [ ] `OLLAMA_NUM_PARALLEL=1` 已生效。
- [ ] 单模型方案的 `OLLAMA_MAX_LOADED_MODELS=1`，双模型方案为 `2`。
- [ ] NexusHarness 容器使用 `--network host`。
- [ ] 安装包和模型包的 SHA-256 与离线交付清单一致。
- [ ] 模型目录同时包含 `blobs` 和 `manifests`。
- [ ] 未在该 32 GB CPU 服务器上启用 `reader-lm` 的 131072 上下文配置。
- [ ] 病历筛选初始并发设置为 `1`。
- [ ] 已记录冷启动、热启动和真实请求耗时。
- [ ] 高峰测试期间没有 OOM、持续 swap 或磁盘长期满载。
- [ ] TCP `11434` 未暴露到公网。

## 17. 推荐配置汇总

双模型兼容方案：

```ini
# /etc/systemd/system/ollama.service.d/override.conf
[Service]
Environment="OLLAMA_HOST=127.0.0.1:11434"
Environment="OLLAMA_MODELS=/data/ollama/models"
Environment="OLLAMA_CONTEXT_LENGTH=4096"
Environment="OLLAMA_NUM_PARALLEL=1"
Environment="OLLAMA_MAX_LOADED_MODELS=2"
Environment="OLLAMA_MAX_QUEUE=8"
Environment="OLLAMA_KEEP_ALIVE=30m"
```

```text
模型：qwen2.5:3b、deepseek-r1:1.5b
Ollama 并行数：1
Ollama 常驻模型数：2
业务并发：1，稳定后最多先试 2
模型目录：/data/ollama/models
监听地址：127.0.0.1:11434
```

## 18. 官方参考

- Ollama Linux 安装：`https://docs.ollama.com/linux`
- Ollama FAQ 和服务端环境变量：`https://docs.ollama.com/faq`
- Ollama API：`https://docs.ollama.com/api/introduction`
- Qwen2.5 模型：`https://ollama.com/library/qwen2.5`
- DeepSeek-R1 模型：`https://ollama.com/library/deepseek-r1`
