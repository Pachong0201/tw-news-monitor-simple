"""Provider 统一接口与结果模型。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional, Protocol


@dataclass
class ProviderResult:
    provider: str
    model: str
    structured_output: dict
    response_id: str = ""
    input_token_count: Optional[int] = 0
    output_token_count: Optional[int] = 0
    total_token_count: Optional[int] = 0
    prompt_cache_hit_tokens: Optional[int] = None
    prompt_cache_miss_tokens: Optional[int] = None
    finish_status: str = ""
    request_duration_ms: int = 0
    provider_warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "provider": self.provider,
            "model": self.model,
            "structured_output": self.structured_output,
            "response_id": self.response_id,
            "input_token_count": self.input_token_count,
            "output_token_count": self.output_token_count,
            "total_token_count": self.total_token_count,
            "prompt_cache_hit_tokens": self.prompt_cache_hit_tokens,
            "prompt_cache_miss_tokens": self.prompt_cache_miss_tokens,
            "finish_status": self.finish_status,
            "request_duration_ms": self.request_duration_ms,
            "provider_warnings": list(self.provider_warnings),
        }


class LLMProvider(Protocol):
    def generate_structured_report(
        self,
        *,
        system_prompt: str,
        user_payload: dict,
        output_schema: dict,
        request_metadata: dict,
    ) -> ProviderResult:
        """Generate a structured report strictly matching output_schema."""
        ...
