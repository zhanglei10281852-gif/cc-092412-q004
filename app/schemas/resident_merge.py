from __future__ import annotations

from pydantic import BaseModel, Field, field_validator


class ResidentMergeRequest(BaseModel):
    source_id: int = Field(gt=0)
    target_id: int = Field(gt=0)
    take_from_source: list[str] = Field(default_factory=list, max_length=20)
    reason: str | None = Field(default=None, max_length=500)

    @field_validator("take_from_source")
    @classmethod
    def unique_fields(cls, value: list[str]) -> list[str]:
        if len(set(value)) != len(value):
            raise ValueError("合并字段不能重复")
        return value
