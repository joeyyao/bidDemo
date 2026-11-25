from typing import Literal
from pydantic import BaseModel, Field

ModelName = Literal["qwen3.8-flash", "qwen3.8-max"]

class Credentials(BaseModel):
    username: str = Field(min_length=3, max_length=80)
    password: str = Field(min_length=6, max_length=128)
class TokenOut(BaseModel):
    access_token: str
    token_type: str = "bearer"
    username: str
class ContractCreate(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    contract_number: str = Field(min_length=1, max_length=120)
    requirements: str = Field(min_length=10, max_length=20000)
    planner_model: ModelName = "qwen3.8-flash"
    chapter_model: ModelName = "qwen3.8-flash"
    merge_model: ModelName = "qwen3.8-flash"
    final_check_model: ModelName = "qwen3.8-flash"
class ChapterCreate(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    description: str = Field(min_length=1, max_length=5000)
class ChapterTitleUpdate(BaseModel):
    title: str = Field(min_length=1, max_length=200)
class ChapterOrderUpdate(BaseModel):
    chapter_ids: list[int] = Field(min_length=1, max_length=50)
class PromptUpdate(BaseModel):
    prompt: str = Field(min_length=1, max_length=10000)
class ModelUpdate(BaseModel):
    model: ModelName
class StageModelUpdate(BaseModel):
    stage: Literal["planner", "chapter_default", "merge", "final_check"]
    model: ModelName
