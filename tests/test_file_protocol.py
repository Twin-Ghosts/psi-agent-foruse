"""``inject_file_protocol`` 的行为断言。

为什么需要它: 交付物「按会话累计」这条链路 (AI 输出 ``[SEND:/path]`` → 扫描成
FileChunk → blob 事件 → 界面累计) 本身是好的, 断的是**模型不知道该输出这个标记**
——默认 agent 的 system prompt 是空字符串。协议注入是这条链路的起点, 注入没生效
就整条不工作, 而这种失败在界面上只表现为"交付物列表一直是空的", 很难定位。

其中「助手历史含 [SEND:] 仍要注入」那条是回归测试: 去重若扫全部消息找 ``[SEND:``,
助手上一轮交付过文件就会让第二轮误判成"讲过了"而跳过注入, 模型反而在多轮会话里
失去协议说明。
"""

from __future__ import annotations

from psi_agent.ai.attachments import (
    _FILE_PROTOCOL,
    _FILE_PROTOCOL_MARK,
    inject_file_protocol,
)


def _system_messages(messages: list) -> list:
    return [m for m in messages if isinstance(m, dict) and m.get("role") == "system"]


def _protocol_in_first(messages: list) -> bool:
    """协议是否在首条消息里。

    **刻意不断言字面量 ``[SEND:``**: 协议说明里绝不能出现完整标记。SEND_RE 是
    ``\\[\\s*SEND\\s*:\\s*(.+?)\\s*\\]``, 扫描器见到就当一次真交付, 而模型复述协议
 ("你可以用 [SEND:/路径] 这样的格式…") 是常见行为 —— 一旦复述就会触发一次
    指向不存在路径的交付, 用户看到一个读不出来的空气交付物。
    """
    return _FILE_PROTOCOL_MARK in messages[0].get("content", "")


def test_protocol_text_never_contains_literal_marker() -> None:
    """协议说明本身不得含完整标记 —— 否则模型复述即触发无效交付。"""
    assert "[SEND:" not in _FILE_PROTOCOL
    assert _FILE_PROTOCOL_MARK in _FILE_PROTOCOL


def test_inserts_system_message_when_absent() -> None:
    out = inject_file_protocol([{"role": "user", "content": "hi"}])
    assert out[0]["role"] == "system"
    assert _protocol_in_first(out)
    assert len(out) == 2


def test_merges_into_empty_system_message() -> None:
    """空 system 消息要合并进去, 不能再插一条 —— 两条 system 消息部分模型会报错。"""
    out = inject_file_protocol(
        [{"role": "system", "content": ""}, {"role": "user", "content": "hi"}]
   )
    assert len(_system_messages(out)) == 1
    assert _protocol_in_first(out)
    assert len(out) == 2


def test_appends_and_preserves_existing_prompt() -> None:
    """workspace 自定义的 system prompt 必须原样保留。"""
    out = inject_file_protocol(
        [{"role": "system", "content": "你是助手"}, {"role": "user", "content": "hi"}]
   )
    assert "你是助手" in out[0]["content"]
    assert _protocol_in_first(out)


def test_idempotent() -> None:
    once = inject_file_protocol(
        [{"role": "system", "content": "你是助手"}, {"role": "user", "content": "hi"}]
   )
    twice = inject_file_protocol(once)
    assert twice[0]["content"] == once[0]["content"]
    assert len(_system_messages(twice)) == 1


def test_empty_messages_untouched() -> None:
    assert inject_file_protocol([]) == []


def test_injects_even_when_assistant_history_mentions_send() -> None:
    """回归: 助手历史里的 [SEND:] 不能被当成"协议已讲过"。

    去重必须只看 system 消息里的协议标记。扫全部消息的话, AI 交付过一次文件之后
    的每一轮都会跳过注入 —— 恰好在最需要协议的多轮会话里失效。
    """
    history = [
        {"role": "system", "content": "你是助手"},
        {"role": "user", "content": "做个表"},
        {"role": "assistant", "content": "好了 [SEND:/w/a.xlsx]"},
        {"role": "user", "content": "再做一个"},
    ]
    out = inject_file_protocol(history)
    assert _protocol_in_first(out)
    assert "你是助手" in out[0]["content"]


def test_tolerates_malformed_entries() -> None:
    """非 dict 项与 content=None 不能让整条转发挂掉。"""
    out = inject_file_protocol(["junk", {"role": "user", "content": None}])
    assert out
    assert out[0]["role"] == "system"


# ---- 注入门控 (ai/server.py 里 `if body.get("tools")` 那一层) ----------------
#
# 协议注入必须**只在这轮带工具时**发生。两类请求都得拦住, 且都是实测踩到的:
#
#   1. 标题/摘要生成走同一个 AI 后端 (gateway/_title_manager 直接 POST 到本
#      socket), body 里没有 tools。给它们讲交付协议, 生成标题的模型也会学会写
#      标记, 标记可能被吐进标题。
#   2. 没有任何工具的 agent (workspace 里没有 tools/ 时就是这样) 根本无法落盘。
#      教它"交付文件"只会让它编一个不存在的路径 —— 实测模型谎称「已调用工具
#      创建」, 然后交付一个读不出来的空气文件。


def _gate(body: dict) -> list:
    """复刻 ai/server.py 的门控判断, 避免为此起一个 aiohttp 应用。"""
    messages = body.get("messages", [])
    return inject_file_protocol(messages) if body.get("tools") else messages


def _mentions_protocol(messages: list) -> bool:
    return any(_FILE_PROTOCOL_MARK in str(m.get("content", "")) for m in messages if isinstance(m, dict))


def test_gate_injects_when_tools_present() -> None:
    out = _gate(
        {
            "messages": [{"role": "system", "content": "你是助手"}, {"role": "user", "content": "做个表"}],
            "tools": [{"type": "function", "function": {"name": "write_file"}}],
        }
   )
    assert _mentions_protocol(out)


def test_gate_skips_title_generation() -> None:
    """标题生成没有 tools, 不能被注入, 且消息数不变。"""
    body = {"messages": [{"role": "user", "content": "Generate a short title..."}]}
    out = _gate(body)
    assert not _mentions_protocol(out)
    assert len(out) == 1


def test_gate_skips_agent_without_tools() -> None:
    """tools 为空列表 (workspace 无 tools/) 时不注入 —— 否则诱导模型编路径。"""
    out = _gate({"messages": [{"role": "user", "content": "给我个文件"}], "tools": []})
    assert not _mentions_protocol(out)
