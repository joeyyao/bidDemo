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

class FinalDocumentTableCell(BaseModel):
    source_cell_index: int | None = Field(default=None, ge=0)
    text: str = Field(default="", max_length=100000)
    original_text: str = Field(default="", max_length=100000)
    colspan: int = Field(default=1, ge=1)
    rowspan: int = Field(default=1, ge=0)
    display: bool = True
    placeholder: bool = False

class FinalDocumentBlock(BaseModel):
    kind: Literal["heading", "subheading", "paragraph", "table", "source_html", "signature"] = "paragraph"
    text: str = Field(default="", max_length=100000)
    source_table_index: int | None = Field(default=None, ge=0)
    source_table_indices: list[int] = Field(default_factory=list)
    source_element_indices: list[int] = Field(default_factory=list)
    rows: list[list[FinalDocumentTableCell]] = Field(default_factory=list)
    header_row: bool = False
    column_alignments: list[Literal["left", "center", "right"]] = Field(default_factory=list)

class FinalDocumentSection(BaseModel):
    chapter_id: int | None = None
    title: str = Field(max_length=200)
    is_cover: bool = False
    source_material: bool = False
    blocks: list[FinalDocumentBlock] = Field(default_factory=list)

class FinalDocumentUpdate(BaseModel):
    header: str = Field(default="", max_length=500)
    footer: str = Field(default="", max_length=500)
    page_number_format: str = Field(default="第 {page} 页", max_length=120)
    cover_title: str = Field(default="", max_length=200)
    contract_number: str = Field(default="", max_length=120)
    template_header: str = Field(default="", max_length=500)
    template_footer: str = Field(default="", max_length=500)
    template_page_number_format: str = Field(default="", max_length=120)
    tender_layout_revision: str = Field(default="", max_length=64)
    sections: list[FinalDocumentSection] = Field(min_length=1, max_length=51)
