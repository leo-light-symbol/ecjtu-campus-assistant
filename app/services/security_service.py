"""输入安全检测与 /api/ask 请求拦截，提供提示词注入防护。"""

from __future__ import annotations

import json
import logging
import re
import unicodedata
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    # 仅在类型检查时依赖 Starlette，检测函数可独立测试和复用。
    from starlette.types import ASGIApp, Receive, Scope, Send

logger = logging.getLogger(__name__)

MAX_QUESTION_LENGTH = 2000
MAX_REQUEST_BODY_BYTES = 64 * 1024
REJECT_MESSAGE = "请求非法，拒绝执行"

# 清理不可见控制字符、双向排版控制符和零宽字符，防止利用隐藏字符绕过检测。
_CONTROL_CHARACTERS = re.compile(
    r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f"
    r"\u200b-\u200f\u202a-\u202e\u2060-\u2064\ufeff\ufffd]"
)
# 过滤基础标记符号，避免问题被二次渲染时形成 HTML 或 Markdown 代码块。
_FILTERED_MARKUP_CHARACTERS = re.compile(r"[<>`]")
_WHITESPACE = re.compile(r"\s+")

# 每条规则均限定“操作意图 + 指令目标”，避免仅凭单个普通词语判断。
# 中文规则限制目标距离，英文规则覆盖常见的指令优先级和身份篡改表达。
_INJECTION_RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "忽略、覆盖或绕过既有指令",
        re.compile(
            r"(?:忽略|无视|跳过|覆盖|取消|废除)[^。！？\n]{0,30}"
            r"(?:指令|指示|规则|要求|提示词|限制|约束)"
            r"|(?:ignore|disregard|override|bypass|skip|cancel)\s+"
            r"(?:all\s+|any\s+|the\s+|your\s+|our\s+|previous\s+|prior\s+|"
            r"above\s+|system\s+|developer\s+|existing\s+|original\s+|safety\s+)*"
            r"(?:instructions?|rules?|prompts?|constraints?|restrictions?|guidelines?)",
            re.IGNORECASE,
        ),
    ),
    (
        "忘记或丢弃规则与初始设定",
        re.compile(
            r"(?:忘记|忘掉|丢弃|抛弃)[^。！？\n]{0,20}"
            r"(?:规则|指令|设定|身份|提示词|限制)"
            r"|forget\s+(?:all\s+|the\s+|your\s+|previous\s+|prior\s+|system\s+)*"
            r"(?:rules?|instructions?|prompts?|identity|constraints?)",
            re.IGNORECASE,
        ),
    ),
    (
        "修改助手身份或进入越狱模式",
        re.compile(
            r"(?:你现在是|你不再是|请扮演|扮演成|假装你是|改变你的身份|"
            r"修改你的身份|重新定义你的身份|切换你的身份|进入开发者模式|"
            r"进入无限制模式|解除所有限制|越狱模式)"
            r"|(?:act|pretend)\s+as\s+(?:a\s+|an\s+|the\s+)?"
            r"|you\s+are\s+now\s+"
            r"|from\s+now\s+on\s*,?\s*(?:you\s+are|act\s+as)"
            r"|change\s+your\s+(?:identity|role)"
            r"|\b(?:jailbreak|developer\s+mode|do\s+anything\s+now)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "窃取系统提示词或内部规则",
        re.compile(
            r"(?:输出|显示|打印|泄露|透露|复述|返回|告诉我|展示)[^。！？\n]{0,25}"
            r"(?:系统提示(?:词)?|系统消息|开发者提示(?:词)?|内部提示(?:词)?|"
            r"内部规则|初始指令|隐藏指令)"
            r"|(?:reveal|show|print|output|repeat|disclose|tell\s+me)\s+"
            r"(?:all\s+|the\s+|your\s+|full\s+|complete\s+|original\s+|hidden\s+)*"
            r"(?:system\s+prompt|developer\s+message|hidden\s+instructions?|"
            r"initial\s+instructions?|internal\s+rules?)",
            re.IGNORECASE,
        ),
    ),
    (
        "执行代码、命令或脚本",
        re.compile(
            r"(?:执行|运行|调用|启动|加载)[^。！？\n]{0,20}"
            r"(?:代码|命令|脚本|程序|shell|cmd|powershell|python|javascript|sql)"
            r"|(?:execute|run|eval|evaluate|invoke|launch)\s+"
            r"(?:all\s+|the\s+|this\s+|following\s+|arbitrary\s+|system\s+)*"
            r"(?:code|commands?|scripts?|programs?|shell|powershell|python|javascript|sql)",
            re.IGNORECASE,
        ),
    ),
    (
        "删除、清空或破坏数据",
        re.compile(
            r"(?:删除|清空|销毁|移除|格式化)[^。！？\n]{0,20}"
            r"(?:数据|数据库|文件|记录|向量库|文档|用户|磁盘|系统)"
            r"|(?:删库|格式化磁盘)"
            r"|(?:delete|drop|truncate|remove|wipe|erase|destroy|format)\s+"
            r"(?:all\s+|the\s+|my\s+|our\s+|entire\s+|system\s+)*"
            r"(?:data|database|tables?|records?|files?|documents?|users?|disks?|systems?)",
            re.IGNORECASE,
        ),
    ),
)


class _RequestBodyTooLarge(ValueError):
    """请求体超过安全读取上限。"""


def sanitize_input(text: str) -> str:
    """规范化文本并过滤危险字符，保留正常中文标点及可读内容。"""
    if not isinstance(text, str):
        raise TypeError("输入必须是字符串")

    # NFKC 将全角字母和兼容字符归一化，降低同形变体绕过规则的概率。
    normalized = unicodedata.normalize("NFKC", text)
    normalized = _CONTROL_CHARACTERS.sub("", normalized)
    normalized = _FILTERED_MARKUP_CHARACTERS.sub("", normalized)
    return _WHITESPACE.sub(" ", normalized).strip()


def check_prompt_injection(text: str) -> bool:
    """检测提示词注入特征；命中任一规则返回 True，否则返回 False。"""
    if not isinstance(text, str) or not text.strip():
        return False

    normalized = sanitize_input(text).lower()
    return any(pattern.search(normalized) for _, pattern in _INJECTION_RULES)


async def _send_json_error(send: Send, status_code: int, detail: str) -> None:
    """直接发送符合 FastAPI 错误结构的 JSON 响应。"""
    body = json.dumps({"detail": detail}, ensure_ascii=False).encode("utf-8")
    await send(
        {
            "type": "http.response.start",
            "status": status_code,
            "headers": [
                (b"content-type", b"application/json; charset=utf-8"),
                (b"content-length", str(len(body)).encode("ascii")),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})


async def _read_request_body(receive: Receive) -> bytes | None:
    """有界读取请求体；客户端断开时返回 None，防止超大请求耗尽内存。"""
    body = bytearray()
    while True:
        message = await receive()
        if message["type"] == "http.disconnect":
            return None
        if message["type"] != "http.request":
            continue

        body.extend(message.get("body", b""))
        if len(body) > MAX_REQUEST_BODY_BYTES:
            raise _RequestBodyTooLarge
        if not message.get("more_body", False):
            return bytes(body)


class PromptInjectionMiddleware:
    """仅拦截 /api/ask，并向后续路由重放清洗后的 JSON 请求体。"""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "").rstrip("/")
        if path != "/api/ask":
            await self.app(scope, receive, send)
            return

        # 先检查声明长度，再读取实际上传内容，两层限制避免异常 Content-Length 绕过。
        content_length = next(
            (
                value
                for name, value in scope.get("headers", [])
                if name.lower() == b"content-length"
            ),
            None,
        )
        try:
            if content_length is not None and int(content_length) > MAX_REQUEST_BODY_BYTES:
                await _send_json_error(send, 413, "请求内容过大")
                return
        except ValueError:
            # 非法长度头交由下游框架处理，实际读取仍有大小上限。
            pass

        try:
            body = await _read_request_body(receive)
        except _RequestBodyTooLarge:
            await _send_json_error(send, 413, "请求内容过大")
            return
        if body is None:
            return

        forward_body = body
        forward_scope = scope
        try:
            payload = json.loads(body)
        except (json.JSONDecodeError, UnicodeDecodeError):
            # 非 JSON 请求不参与问答，保留下来的格式错误由 FastAPI 统一校验。
            payload = None

        if isinstance(payload, dict) and isinstance(payload.get("question"), str):
            question = payload["question"]
            if len(question) > MAX_QUESTION_LENGTH:
                await _send_json_error(send, 413, "问题长度不能超过 2000 个字符")
                return

            cleaned_question = sanitize_input(question)
            if len(cleaned_question) > MAX_QUESTION_LENGTH:
                await _send_json_error(send, 413, "问题长度不能超过 2000 个字符")
                return
            if check_prompt_injection(cleaned_question):
                logger.warning("已拦截 /api/ask 提示词注入请求")
                await _send_json_error(send, 403, REJECT_MESSAGE)
                return

            # 将过滤后的问题写回请求体，确保后续 RAG 服务使用安全文本。
            payload["question"] = cleaned_question
            forward_body = json.dumps(
                payload,
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")

            # 请求体变化后同步更新 Content-Length，避免下游读取长度不一致。
            forward_scope = dict(scope)
            forward_scope["headers"] = [
                (name, value)
                for name, value in scope.get("headers", [])
                if name.lower() != b"content-length"
            ] + [(b"content-length", str(len(forward_body)).encode("ascii"))]

        body_sent = False

        async def replay_receive():
            """第一次重放完整请求体，后续通知下游请求已结束。"""
            nonlocal body_sent
            if not body_sent:
                body_sent = True
                return {
                    "type": "http.request",
                    "body": forward_body,
                    "more_body": False,
                }
            return {"type": "http.disconnect"}

        await self.app(forward_scope, replay_receive, send)
