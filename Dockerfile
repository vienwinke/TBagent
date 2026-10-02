# ============================================================================
# 数据问答 Agent —— Streamlit 演示镜像
#
# 为什么需要它：docs/部署.md 一直在教 `docker build`，而仓库里没有 Dockerfile。
#
# ⚠️ 状态：**本机未能构建验证**（WSL 里没开 Docker Desktop 集成，docker 不可用）。
#    写法按 python:3.12-slim + requirements.lock 逐字节复现，但请以 CI 或本地
#    真实构建为准；首次构建后建议把 `docker run` 的冒烟命令补进部署文档。
#
# 基础镜像取 3.12 而不是本地的 3.14：3.14 目前没有 torch 轮子，
# 本地向量检索（requirements-vector.txt）在 3.12 上才有完整轮子。
# ============================================================================
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    STREAMLIT_SERVER_HEADLESS=true \
    STREAMLIT_BROWSER_GATHER_USAGE_STATS=false \
    DB_BACKEND=sqlite

WORKDIR /app

# 先装依赖再拷代码：改代码不会让依赖层失效
COPY requirements.lock ./
RUN pip install --no-cache-dir -r requirements.lock

COPY . .

# 非 root 运行
RUN useradd --create-home --uid 10001 appuser && chown -R appuser:appuser /app
USER appuser

EXPOSE 8501

# 健康检查打 Streamlit 的 /_stcore/health（官方推荐路径）
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8501/_stcore/health', timeout=4).status == 200 else 1)"

CMD ["streamlit", "run", "app.py", "--server.port=8501", "--server.address=0.0.0.0"]
