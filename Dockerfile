# psi-agent-auth
#
# 当前用标准库 http.server（这个开发环境没有 FastAPI/uvicorn/aiohttp）。
# 方案文档要求 aiohttp + anyio，替换 app/server.py 即可，service.py 与
# store.py 不动 —— 契约测试是传输无关的，换实现后同一套测试仍然适用。

FROM python:3.12-slim

# 不装编译工具链：当前依赖全是标准库
WORKDIR /app

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
