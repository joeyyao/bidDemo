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
    stage: Literal["planner", "chapter_default"]
    model: ModelName

class FinalDocumentBlock(BaseModel):
    kind: Literal["heading", "subheading", "paragraph"] = "paragraph"
    text: str = Field(max_length=100000)

class FinalDocumentSection(BaseModel):
    chapter_id: int | None = None
    title: str = Field(max_length=200)
    is_cover: bool = False
    blocks: list[FinalDocumentBlock] = Field(default_factory=list, max_length=300)

class FinalDocumentUpdate(BaseModel):
    header: str = Field(default="", max_length=500)
    footer: str = Field(default="", max_length=500)
    page_number_format: str = Field(default="第 {page} 页", max_length=120)
    cover_title: str = Field(default="", max_length=200)
    contract_number: str = Field(default="", max_length=120)
    sections: list[FinalDocumentSection] = Field(min_length=1, max_length=50)
