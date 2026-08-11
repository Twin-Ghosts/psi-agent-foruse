# psi-agent-auth
#
# HTTP 层已按方案文档改为 aiohttp + anyio（server.py）；service.py 与
# store.py 不动 —— 契约测试是传输无关的，换实现后同一套测试仍然适用。
# 依赖仅 aiohttp + anyio（其余全是标准库），见 requirements.txt。

FROM python:3.12-slim

WORKDIR /app

# 先装依赖（利用层缓存：requirements 不变时不重装）
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY . /app

# 以非 root 运行
RUN useradd -r -u 10001 -m appuser \
	&& mkdir -p /data \
	&& chown -R appuser:appuser /app /data
USER appuser

ENV PYTHONUNBUFFERED=1 \
	PYTHONDONTWRITEBYTECODE=1 \
	PYTHONUTF8=1 \
	AUTH_DB=/data/auth.db

EXPOSE 8000

# 注意：不带 --test-hooks。测试钩子会回显验证码，生产必须关闭。
CMD ["python", "-m", "app.server"]
