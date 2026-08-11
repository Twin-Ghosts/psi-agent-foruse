from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any, cast

import anyio
from aiohttp import web
from any_llm.api import ChatCompletionChunk, acompletion
from loguru import logger

from psi_agent.ai.attachments import expand_attachments, inject_file_protocol


async def handle_chat_completions(request: web.Request) -> web.StreamResponse:
    logger.info("Received chat completion request")
    try:
        body: dict[str, Any] = await request.json()
        logger.debug(f"Request body: {json.dumps(body, ensure_ascii=False)[:1000]}")
    except Exception as e:
        logger.error(f"Failed to parse request body: {e!r}")
        # OpenAI-compatible error response.
        return web.json_response(
            {"error": {"message": str(e), "type": "invalid_request_error", "param": None, "code": 400}},
            status=400,
       )

    provider = request.app["provider"]
    model = request.app["model"]
    api_key = request.app["api_key"]
    base_url = request.app["base_url"]

    logger.debug(f"Body keys before pop: {list(body)}")
    messages = body.pop("messages", [])
    # 把附件标记 [RECV:/path] 展开成模型可消费的内容: 能看图的模型给多模态图片块,
    # 否则抽取文字注入。此前模型只收到一串路径、看不到附件内容。
    try:
        messages = expand_attachments(messages, provider, model)
        # 只在**这轮带工具**时才讲 [SEND:] 交付协议。两个都必须拦住:
        #
        # 1. 标题/摘要生成走同一个后端 (gateway/_title_manager 直接 POST 到本 socket),
        #    body 里没有 tools。给它们讲交付协议会让生成标题的模型也学会写标记,
        #    标记可能被吐进标题里。
        # 2. 没有任何工具的 agent (workspace 无 tools/ 时就是这样) 根本没法落盘。
        #    教它"交付文件"只会让它编一个不存在的路径 —— 实测模型会谎称
        #    「已调用工具创建」, 然后交付一个空气文件。
        if body.get("tools"):
            messages = inject_file_protocol(messages)
    except Exception as e:
        logger.warning(f"附件处理/协议注入失败, 按原样转发: {e!r}")
    body.pop("stream", None)
    body.pop("provider", None)
    body.pop("model", None)
    body.pop("api_key", None)
    body.pop("api_base", None)
    body.pop("routing", None)
    stream_opts = body.get("stream_options", {})
    if isinstance(stream_opts, dict):
        stream_opts["include_usage"] = True
        body["stream_options"] = stream_opts
    else:
        body["stream_options"] = {"include_usage": True}
    logger.debug(f"Body keys to passthrough: {list(body)}")

    response = web.StreamResponse(
        status=200,
        reason="OK",
        headers={
            # SSE standard headers — per MDN / HTML spec
            "Content-Type": "text/event-stream",
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
   )
    try:
        await response.prepare(request)
    except Exception:
        logger.warning("Client disconnected before SSE response prepared")
        return response

    logger.debug(f"Forwarding to upstream: provider={provider!r}, model={model!r}, base_url={base_url!r}")
    upstream_error = False
    client_gone = False
    compaction_needed = False
    stream: AsyncIterator[ChatCompletionChunk] | None = None
    try:
        stream = cast(
            AsyncIterator[ChatCompletionChunk],
            # ``acompletion()`` returns ``ChatCompletion | AsyncIterator[ChatCompletionChunk]``
            # depending on the ``stream`` flag.  We always pass ``stream=True``, so the
            # runtime type is always ``AsyncIterator[ChatCompletionChunk]`` — the cast is safe.
            await acompletion(
                provider=provider,
                model=model,
                messages=messages,
                stream=True,
                api_key=api_key,
                api_base=base_url,
                **body,
           ),
       )
        logger.debug("Starting to consume upstream SSE stream")
        max_context_tokens: int = request.app.get("max_context_tokens", 0)
        compaction_usage: dict[str, int] = {}
        async for chunk in stream:
            if max_context_tokens > 0 and chunk.usage and chunk.usage.prompt_tokens > max_context_tokens:
                compaction_needed = True
                compaction_usage = {
                    "prompt_tokens": chunk.usage.prompt_tokens,
                    "completion_tokens": chunk.usage.completion_tokens,
                    "total_tokens": chunk.usage.total_tokens,
                }
                logger.debug(
                    f"Compaction needed: prompt_tokens={chunk.usage.prompt_tokens} > threshold={max_context_tokens}"
               )
            data = chunk.model_dump_json()
            logger.debug(f"SSE chunk: {data[:1000]}")
            await response.write(f"data: {data}\n\n".encode())
        if compaction_needed:
            signal = json.dumps(
                {
                    "id": "compaction",
                    "choices": [{"index": 0, "delta": {}, "finish_reason": "compaction_needed"}],
                    "psi_compaction": {
                        "needed": True,
                        "prompt_tokens": compaction_usage.get("prompt_tokens", 0),
                        "threshold": max_context_tokens,
                    },
                }
           )
            logger.debug(f"SSE compaction signal: {signal[:500]}")
            await response.write(f"data: {signal}\n\n".encode())
    except ConnectionResetError:
        # Downstream client (session/channel) disconnected — e.g. user pressed
        # "stop". The finally block closes the upstream provider stream.
        client_gone = True
        logger.info("Client disconnected; cancelling upstream stream")
    except Exception as e:
        upstream_error = True
        logger.error(f"Error forwarding to upstream (provider={provider!r}, model={model!r}): {e!r}")
        err_chunk = json.dumps(
            {
                "id": "error",
                "choices": [{"index": 0, "delta": {"content": f"[Upstream Error]: {e}"}, "finish_reason": "error"}],
            }
       )
        logger.debug(f"SSE error chunk: {err_chunk[:1000]}")
        try:
            await response.write(f"data: {err_chunk}\n\n".encode())
        except Exception:
            logger.warning("Failed to send upstream error chunk to client")
    else:
        if compaction_needed:
            logger.debug("Request completed with compaction signal")
        else:
            logger.debug("Upstream stream completed successfully")
    finally:
        # Always release the upstream connection, even on cancellation
        # (client disconnect / shutdown). Shielded so aclose() completes
        # while a CancelledError is propagating through this finally.
        if stream is not None:
            aclose = getattr(stream, "aclose", None)
            if aclose is not None:
                logger.debug("Closing upstream stream")
                with anyio.CancelScope(shield=True):
                    try:
                        await aclose()
                    except Exception as close_err:
                        logger.warning(f"Failed to close upstream stream: {close_err}")

    if client_gone:
        logger.info("Request cancelled by client disconnect")
    elif upstream_error:
        logger.info("Request completed with upstream error")
    else:
        logger.info("Request completed successfully")
    return response
