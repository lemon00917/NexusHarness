# Ollama 内网离线部署

部署包已经准备好，适用于 16 核 CPU、32 GB 内存、无 GPU、机械硬盘的 Linux 服务器。
以下步骤全部适用于内网环境，不需要服务器访问公网。

## 一、传到内网服务器

把 `ollama-offline` 整个目录传到 Linux，例如放到：

```text
/opt/offline/ollama/
```

确认目录内有以下三个主要文件：

```text
ollama-linux-amd64.tar.zst
ollama-models.tar
install_ollama_offline.sh
```

模型包已经包含 `qwen2.5:3b` 和 `deepseek-r1:1.5b`，服务器不需要联网下载模型。

## 二、运行安装脚本

直接进入部署目录运行安装脚本：

```bash
cd /opt/offline/ollama
sudo bash install_ollama_offline.sh
```

脚本会自动检查 `zstd`。openEuler 会优先使用 `dnf`，如果系统没有 `dnf` 则尝试 `yum`。包管理器只会使用服务器现有的软件源，请确认该软件源是内网源。

如果想先手动安装，在 openEuler 上执行：

```bash
sudo dnf install -y zstd
```

如果提示没有 `dnf`，再执行：

```bash
sudo yum install -y zstd
```

如果内网没有软件源，不能执行在线安装。请让运维把 `zstd` 的 RPM 包及依赖放到服务器，再执行：

```bash
sudo rpm -Uvh /path/to/zstd*.rpm
```

如果服务器完全离线且没有可用软件源，脚本会提示安装失败。这时需要让运维提前提供 `zstd` 安装包：

```bash
# openEuler/CentOS/RHEL/麒麟/统信等 RPM 系统
sudo rpm -Uvh zstd*.rpm

# Ubuntu/Debian 等 DEB 系统
sudo dpkg -i zstd*.deb
```

安装 `zstd` 后，再重新执行上面的安装脚本即可。安装脚本、Ollama 和两个模型都在部署目录中，不会联网下载。

看到下面内容表示安装完成：

```text
Installation complete
API: http://127.0.0.1:11434
```

## 三、测试

```bash
ollama run qwen2.5:3b "只回答：安装成功"
```

## 四、项目配置

最简单、最快的方案是全部使用一个模型：

```text
router_model=qwen2.5:3b
judge_model=qwen2.5:3b
planner_model=qwen2.5:3b
```

NexusHarness 如果在同一台服务器的 Docker 中运行，需要使用宿主机网络：

```bash
--network host
```

服务默认只监听 `127.0.0.1:11434`，不要开放公网端口。
