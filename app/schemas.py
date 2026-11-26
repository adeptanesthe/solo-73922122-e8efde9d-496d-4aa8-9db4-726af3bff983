"""FastAPI 请求模型。所有厚度与步长都要求严格的整数（拒绝 bool/float/字符串）。"""

from __future__ import annotations

from typing import Any, Union

from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator

InstrumentId = Union[StrictInt, str]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RangeModel(StrictModel):
    """两层厚度与总厚度的整数闭区间。"""

    outer: tuple[StrictInt, StrictInt]
    inner: tuple[StrictInt, StrictInt]
    total: tuple[StrictInt, StrictInt]

    @field_validator("outer", "inner", "total")
    @classmethod
    def _non_negative(cls, value: tuple[int, int]) -> tuple[int, int]:
        if value[0] < 0 or value[1] < 0:
            raise ValueError("厚度范围必须是非负整数")
        return value


class InstrumentModel(StrictModel):
    id: InstrumentId
    step: StrictInt = Field(gt=0)

    @field_validator("id")
    @classmethod
    def _non_empty_id(cls, value: Union[int, str]) -> Union[int, str]:
        if isinstance(value, str) and value.strip() == "":
            raise ValueError("仪表编号不能为空字符串")
        return value


class TreeModel(StrictModel):
    """判定树用自由 JSON 传递，由引擎做结构校验（叶节点数等）。"""

    tree: dict[str, Any]


class PlanRequest(StrictModel):
    coarse: RangeModel
    tree: dict[str, Any]
    instruments: list[InstrumentModel]

    @field_validator("instruments")
    @classmethod
    def _unique_ids(
        cls, value: list[InstrumentModel]
    ) -> list[InstrumentModel]:
        seen: set[Union[int, str]] = set()
        for item in value:
            if item.id in seen:
                raise ValueError(f"仪表编号重复: {item.id!r}")
            seen.add(item.id)
        return value


class AssessRequest(PlanRequest):
    pass


class PrecisionRequest(StrictModel):
    instrument_id: InstrumentId
    reading: StrictInt

    @field_validator("reading")
    @classmethod
    def _non_negative(cls, value: int) -> int:
        if value < 0:
            raise ValueError("实测档值必须是非负整数")
        return value
