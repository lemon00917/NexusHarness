# NexusHarness Docker Image
# ============================
# 应用容器，连接外部 Ollama（Ollama 独立部署，本容器只运行应用）
#
# 构建：
#   docker build -t nexusharness:latest .
#
# 导出离线包：
#   docker save -o nexusharness.tar nexusharness:latest
#   gzip nexusharness.tar
#
# 内网加载：
#   docker load -i nexusharness.tar.gz
#
# 运行：
#   docker run -d --name nexusharness --network host \
#     -e OLLAMA_HOST=http://localhost:11434 \
#     -v /imedical/cdr/ai/nexus/data:/app/data \
#     -v /imedical/cdr/ai/nexus/configs:/app/configs \
#     -v /imedical/cdr/ai/nexus/logs:/app/logs \
#     -v /imedical/cdr/ai/nexus/web/rag_index:/app/web/rag_index \
#     nexusharness:latest

FROM python:3.12

LABEL maintainer="NexusHarness"
LABEL description="NexusHarness - Minimal Agent Harness with RAG"

# 完整版 python:3.12 镜像自带 gcc/g++/libgomp1，无需 apt-get
# （内网 HTTP 80 端口被封，apt 源走 HTTP 协议不可用，slim 转完整版绕过）

WORKDIR /app

# ── 第一层：安装 Python 依赖（利用 Docker 层缓存） ──────────────
# 全局设置阿里源
ENV PIP_INDEX_URL=https://mirrors.aliyun.com/pypi/simple/

# pkuseg 在 Docker 内下载极慢，提前下载到项目根目录，从本地安装
# 问题1: setup.py import numpy但pyproject.toml未声明 → 手动装numpy + --no-build-isolation
# 问题2: 老Cython生成的.cpp引用longintrepr.h → Python 3.12已删除 → 用新版Cython重新生成
COPY pkuseg-0.0.25.tar.gz .
RUN pip install --no-cache-dir --timeout=600 --upgrade pip && \
    pip install --no-cache-dir --timeout=600 numpy setuptools wheel cython && \
    tar -xzf pkuseg-0.0.25.tar.gz && \
    cd pkuseg-0.0.25 && \
    python -m cython --cplus -3 pkuseg/inference.pyx && \
    python -m cython --cplus -3 pkuseg/feature_extractor.pyx && \
    python -m cython --cplus -3 pkuseg/postag/feature_extractor.pyx && \
    cd .. && \
    pip install --no-cache-dir --timeout=600 --no-build-isolation ./pkuseg-0.0.25/ && \
    rm -rf pkuseg-0.0.25 pkuseg-0.0.25.tar.gz
COPY requirements.txt .

# 核心依赖（排除 sentence-transformers——默认用 Ollama embedding，不需要它）
RUN grep -vE '^(#|$)' requirements.txt | grep -v 'sentence-transformers' | \
    xargs pip install --no-cache-dir --timeout=600 --timeout=600

# 补充依赖（不在 requirements.txt 中，但代码引用）
RUN pip install --no-cache-dir --timeout=600 --timeout=600 html-to-markdown

# ── 第二层：复制应用代码 ──────────────────────────────────────────
COPY microharness/ ./microharness/
COPY web/ ./web/
COPY data/ ./data/
COPY skills/ ./skills/
COPY configs/ ./configs/
COPY harness.py ./

# XML templates must survive volume mounts — copy to a non-mounted location
RUN mkdir -p /app/templates_xml && cp -r /app/data/临床文档模板/* /app/templates_xml/ 2>/dev/null || true

# ── 运行时目录 ────────────────────────────────────────────────────
RUN mkdir -p \
    conversations \
    sessions \
    logs \
    cache \
    results \
    web/rag_index

# ── 环境变量 ─────────────────────────────────────────────────────
ENV OLLAMA_HOST=http://localhost:11434
ENV PYTHONUNBUFFERED=1
ENV PYTHONIOENCODING=utf-8

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/api/rag/config')" || exit 1

CMD ["python", "-m", "uvicorn", "web.app:app", "--host", "0.0.0.0", "--port", "8000"]
