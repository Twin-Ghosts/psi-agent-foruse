"""埋点入参。字段与原 collector.py 接受的 JSON 键一致(camelCase)。"""

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class EventIn(BaseModel):
    # 原实现对未知字段是静默忽略,这里保持宽容,避免打点端小改就 422
    model_config = ConfigDict(extra="ignore")

    name: str = Field(default="", max_length=128)
    page: str = Field(default="", max_length=512)
    url: str = Field(default="", max_length=2048)
    referrer: str = Field(default="", max_length=2048)
    os: str = Field(default="", max_length=64)
    device: str = Field(default="", max_length=64)
    lang: str = Field(default="", max_length=32)
    clientId: str = Field(default="", max_length=128)
    sessionId: str = Field(default="", max_length=128)
    props: dict[str, Any] = Field(default_factory=dict)
