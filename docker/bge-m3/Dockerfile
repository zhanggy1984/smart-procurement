# ============================================================
# BGE-M3 Embedding 独立服务
# 骨架先行：容器健康检查只依赖进程存活 + /health 端点，
# 模型在首次 /embed 调用时懒加载（P2 阶段文档解析自然触发）。
# ============================================================
FROM python:3.11-slim

RUN groupadd -r app && useradd -r -g app -d /app app

WORKDIR /app

COPY requirements.txt /app/
RUN pip install --no-cache-dir -r requirements.txt && rm -rf /root/.cache/pip

COPY main.py /app/

# HF 模型缓存目录预建并归属 app 用户（非 root 运行才能写入）；
# docker-compose 将 bge-m3-model 卷挂载到此目录持久化模型。
RUN mkdir -p /app/.cache && chown -R app:app /app/.cache

USER app

EXPOSE 8081

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8081"]
