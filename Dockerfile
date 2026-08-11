FROM python:3.14-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/app/src \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# 依赖单独一层,改代码不重装依赖
COPY requirements.txt ./
# 内地机器直连 PyPI 慢,走阿里云镜像。可用 --build-arg PIP_INDEX_URL 覆盖
ARG PIP_INDEX_URL=https://mirrors.aliyun.com/pypi/simple/
RUN pip install --no-cache-dir -i $PIP_INDEX_URL -r requirements.txt

COPY src/ src/

# 非 root 运行。uid 与宿主机 data 目录 owner 对齐(见 deploy/init-perms.sh)
RUN useradd -r -u 10001 -m appuser
USER appuser

EXPOSE 8000
# 默认起认证服务;analytics 在 compose 里覆盖 command
CMD ["uvicorn", "psi_cloud.auth.app:app", "--host", "0.0.0.0", "--port", "8000"]
