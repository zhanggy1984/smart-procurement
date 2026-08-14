# ============================================================
# FastAPI App / arq Worker 共享镜像
# Python 3.11 + 非 root 用户 + 只读挂载代码
# ============================================================
FROM python:3.11-slim

# 非 root 用户（安全基线）
RUN groupadd -r app && useradd -r -g app -d /app app

# 系统依赖：asyncmy 编译 + 文档解析库
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        gcc \
        default-libmysqlclient-dev \
        pkg-config \
        libxml2-dev \
        libxslt1-dev \
        curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# 先装依赖（利用 Docker 层缓存）
# poetry.lock 必须一起 COPY，否则容器内 poetry install 会重新解析出最新版本
# （如 fastapi>=0.115 会解析到 0.141.1，与 pydantic 2.13 存在 import 兼容问题），
# 锁定版本保证容器与本地开发环境一致。
COPY pyproject.toml /app/
COPY poetry.lock /app/
# README 被 pyproject 声明为 readme 字段，poetry 打包当前项目时必须读取，缺失会失败
COPY README.md /app/
COPY app /app/app
RUN pip install --no-cache-dir poetry==2.0.1 \
    && poetry config virtualenvs.create false \
    && poetry install --only main --no-interaction --no-ansi \
    && rm -rf /root/.cache/pip

# 切换非 root 用户
USER app

EXPOSE 8000

# 默认启动 FastAPI（worker 通过 command 覆盖）
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
