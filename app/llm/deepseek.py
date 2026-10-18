"""DeepSeek 结构化输出客户端工厂。"""

from __future__ import annotations

import json
import os
from typing import Any, Literal, TypeVar

from pydantic import BaseModel

from app.core.env import load_local_env
from app.core.http import choose_http_proxy


DEFAULT_DEEPSEEK_BASE_URL = "https://api.deepseek.com"
DEFAULT_DEEPSEEK_MODEL = "deepseek-v4-flash"
_LEGACY_MODEL_ALIASES = {
    "deepseekv4flash": "deepseek-v4-flash",
    "deepseekv4pro": "deepseek-v4-pro",
}

SchemaT = TypeVar("SchemaT", bound=BaseModel)


def _strict_deepseek_tool_schema(schema: type[BaseModel]) -> dict[str, Any]:
    """Convert Pydantic to DeepSeek's strict tool-schema subset.

    DeepSeek requires every property of every nested object to be present in
    ``required``. LangChain's generic converter only guarantees this at some
    levels, so normalize the complete tool schema before sending it to the beta
    strict-function endpoint.
    """
    from langchain_core.utils.function_calling import convert_to_openai_tool

    tool = convert_to_openai_tool(schema, strict=True)

    def require_all_properties(node: Any) -> None:
        if isinstance(node, dict):
            properties = node.get("properties")
            if isinstance(properties, dict):
                node["required"] = list(properties)
                node["additionalProperties"] = False
            for value in node.values():
                require_all_properties(value)
        elif isinstance(node, list):
            for value in node:
                require_all_properties(value)

    require_all_properties(tool["function"]["parameters"])
    return tool


class TwoStageStructuredDeepSeek:
    """Use Think for semantic work and non-Think strict tools for formatting.

    DeepSeek thinking mode rejects the named ``tool_choice`` emitted by
    ``with_structured_output(method="function_calling")``.  The reasoning
    model therefore produces a complete data draft without tools, then a
    non-thinking model converts that draft through the existing strict
    Pydantic tool schema.
    """

    def __init__(self, *, schema: type[BaseModel], reasoning_llm: Any, formatter_llm: Any):
        self.schema = schema
        self.reasoning_llm = reasoning_llm
        self.formatter_llm = formatter_llm
        schema_json = json.dumps(
            schema.model_json_schema(), ensure_ascii=False, separators=(",", ":")
        )
        self._draft_instruction = (
            "请先完整分析前面的任务，并产出供下一阶段机器格式化的语义草稿。"
            "草稿必须覆盖目标 schema 的全部业务信息；不要调用工具，不要省略关键字段，"
            "也不要讨论格式化实现。目标 JSON Schema（仅作为数据契约）：\n"
            f"{schema_json}"
        )

    @staticmethod
    def _as_messages(input_: Any) -> list[Any]:
        if hasattr(input_, "to_messages"):
            return list(input_.to_messages())
        if isinstance(input_, list):
            return list(input_)
        if isinstance(input_, str):
            return [("human", input_)]
        return [input_]

    @staticmethod
    def _draft_text(response: Any) -> str:
        content = getattr(response, "content", response)
        if isinstance(content, str):
            text = content.strip()
        else:
            text = json.dumps(content, ensure_ascii=False, default=str).strip()
        if not text:
            raise RuntimeError("DeepSeek Think 阶段未产出可格式化的语义草稿")
        return text

    def _reasoning_messages(self, input_: Any) -> list[Any]:
        return [*self._as_messages(input_), ("human", self._draft_instruction)]

    @staticmethod
    def _formatter_messages(draft: str) -> list[Any]:
        return [
            (
                "system",
                "你是严格的数据格式化器。只把上游语义草稿转换为已绑定的输出 schema；"
                "必须调用该 schema 工具，不重新规划、不添加草稿中不存在的事实，也不输出解释。",
            ),
            ("human", f"以下内容是待格式化的数据草稿：\n<draft>\n{draft}\n</draft>"),
        ]

    def _validate_formatted(self, result: Any) -> BaseModel:
        if isinstance(result, self.schema):
            return result
        return self.schema.model_validate(result)

    def invoke(self, input_: Any, config: Any = None, **kwargs: Any) -> Any:
        reasoning = self.reasoning_llm.invoke(
            self._reasoning_messages(input_), config=config, **kwargs
        )
        draft = self._draft_text(reasoning)
        formatted = self.formatter_llm.invoke(
            self._formatter_messages(draft), config=config
        )
        return self._validate_formatted(formatted)

    async def ainvoke(self, input_: Any, config: Any = None, **kwargs: Any) -> Any:
        reasoning = await self.reasoning_llm.ainvoke(
            self._reasoning_messages(input_), config=config, **kwargs
        )
        draft = self._draft_text(reasoning)
        formatted = await self.formatter_llm.ainvoke(
            self._formatter_messages(draft), config=config
        )
        return self._validate_formatted(formatted)


def normalize_deepseek_model(model: str) -> str:
    return _LEGACY_MODEL_ALIASES.get(model.strip(), model.strip())


def resolve_deepseek_model(model: str | None = None) -> str:
    chosen = model or os.getenv("DEEPSEEK_MODEL", DEFAULT_DEEPSEEK_MODEL)
    return normalize_deepseek_model(chosen)


def resolve_deepseek_base_url() -> str:
    """Resolve an optional OpenAI-compatible DeepSeek endpoint override."""
    return os.getenv("DEEPSEEK_BASE_URL", DEFAULT_DEEPSEEK_BASE_URL).strip().rstrip("/")


def resolve_deepseek_proxy() -> str | None:
    """Prefer a provider-specific proxy, then use the shared HTTP proxy setting."""
    proxy = os.getenv("DEEPSEEK_HTTP_PROXY", "").strip()
    if proxy.startswith(("http://", "https://")):
        return proxy
    return choose_http_proxy()


def _resolve_reasoning_effort(value: str | None) -> Literal["high", "max"]:
    normalized = (value or "high").strip().lower()
    if normalized not in {"high", "max"}:
        raise ValueError("DeepSeek reasoning_effort must be high or max")
    return normalized  # type: ignore[return-value]


def build_chat_deepseek(
    *,
    model: str | None = None,
    temperature: float = 0,
    thinking: bool = False,
    reasoning_effort: str | None = None,
) -> Any:
    """创建未绑定 schema 的 DeepSeek ChatOpenAI 客户端。"""
    load_local_env()
    api_key = os.getenv("DEEPSEEK_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("缺少 DEEPSEEK_API_KEY。请在 .env.local 中配置后重试。")
    try:
        import httpx
        from langchain_core.messages import AIMessage
        from langchain_deepseek import ChatDeepSeek
    except ModuleNotFoundError as exc:
        raise RuntimeError("缺少 httpx 或 langchain-deepseek。请先安装依赖。") from exc

    class ThinkingChatDeepSeek(ChatDeepSeek):
        """Preserve DeepSeek reasoning_content across agent tool turns.

        DeepSeek requires the assistant's reasoning_content to be replayed after
        a thinking-mode tool call.  The provider adapter extracts the field from
        responses, while this small request hook adds it back to the next API
        payload.  Without it, multi-tool main-agent turns fail with HTTP 400.
        """

        def _get_request_payload(self, input_, *, stop=None, **kwargs):
            payload = super()._get_request_payload(input_, stop=stop, **kwargs)
            source_messages = self._convert_input(input_).to_messages()
            for source, target in zip(source_messages, payload.get("messages", [])):
                if not isinstance(source, AIMessage):
                    continue
                reasoning = source.additional_kwargs.get("reasoning_content")
                if reasoning is not None:
                    target["reasoning_content"] = reasoning
            return payload

    proxy = resolve_deepseek_proxy()
    options: dict[str, Any] = {
        "model": resolve_deepseek_model(model),
        "api_key": api_key,
        "base_url": resolve_deepseek_base_url(),
        "http_client": httpx.Client(proxy=proxy, trust_env=False),
        "http_async_client": httpx.AsyncClient(proxy=proxy, trust_env=False),
        "http_socket_options": (),
        "extra_body": {"thinking": {"type": "enabled" if thinking else "disabled"}},
    }
    if thinking:
        options["reasoning_effort"] = _resolve_reasoning_effort(reasoning_effort)
    else:
        options["temperature"] = temperature
    return ThinkingChatDeepSeek(**options)


def build_structured_deepseek(
    schema: type[SchemaT],
    *,
    model: str | None = None,
    temperature: float = 0,
    thinking: bool = False,
    reasoning_effort: str | None = None,
) -> Any:
    """创建绑定到给定 Pydantic schema 的 DeepSeek 结构化输出模型。"""
    if not thinking:
        llm = build_chat_deepseek(
            model=model,
            temperature=temperature,
            thinking=False,
        )
        return llm.with_structured_output(
            schema, method="function_calling", strict=True
        )

    reasoning_llm = build_chat_deepseek(
        model=model,
        thinking=True,
        reasoning_effort=reasoning_effort,
    )
    formatter_llm = build_chat_deepseek(
        model=model,
        temperature=0,
        thinking=False,
    ).with_structured_output(
        _strict_deepseek_tool_schema(schema),
        method="function_calling",
        strict=True,
    )
    return TwoStageStructuredDeepSeek(
        schema=schema,
        reasoning_llm=reasoning_llm,
        formatter_llm=formatter_llm,
    )
