from pathlib import Path
from datetime import datetime, timezone
from copy import deepcopy
from hashlib import sha256
from urllib.parse import quote
import json
import re
from uuid import uuid4
from fastapi import FastAPI, Depends, HTTPException, BackgroundTasks
from fastapi import File, Form, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session
from sqlalchemy import select, func, inspect, text
from io import BytesIO
from docx import Document
from docx.shared import Inches
from docx.shared import Pt, RGBColor
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from .config import settings
from .database import Base, engine, get_db, SessionLocal
from .models import User, Contract, Chapter, Version, WorkflowEvent
from .schemas import Credentials, TokenOut, PromptUpdate, ModelUpdate, StageModelUpdate, ChapterCreate, ChapterTitleUpdate, ChapterOrderUpdate, ModelName, FinalDocumentUpdate
from .auth import hash_password, verify_password, make_token, current_user
from .generator import workflow, cover_plan, long_prompt, draft_stage, merge_node, normalize_model_text, compact_document_text
from .word_table_format import cell_format, table_format

Path("data").mkdir(exist_ok=True)
Base.metadata.create_all(bind=engine)
with engine.begin() as connection:
    contract_columns = {column["name"] for column in inspect(connection).get_columns("contracts")}
    contract_migrations = {
        "node_models": "TEXT NOT NULL DEFAULT '{}'", "contract_number": "VARCHAR(120) NOT NULL DEFAULT ''",
        "generation_id": "VARCHAR(36) NOT NULL DEFAULT ''",
        "deleted_at": "DATETIME NULL",
        "tender_filename": "VARCHAR(255) NOT NULL DEFAULT ''", "tender_text": "TEXT NOT NULL DEFAULT ''",
        "tender_compact_text": "TEXT NOT NULL DEFAULT ''",
        "tender_file_data": "BLOB NOT NULL DEFAULT X''",
        "tender_layout_filename": "VARCHAR(255) NOT NULL DEFAULT ''",
        "tender_layout_data": "BLOB NOT NULL DEFAULT X''",
        "reference_filename": "VARCHAR(255) NOT NULL DEFAULT ''", "reference_text": "TEXT NOT NULL DEFAULT ''",
        "reference_compact_text": "TEXT NOT NULL DEFAULT ''",
        "final_document_json": "TEXT NOT NULL DEFAULT ''", "document_header": "TEXT NOT NULL DEFAULT ''",
        "document_footer": "TEXT NOT NULL DEFAULT ''", "page_number_format": "VARCHAR(120) NOT NULL DEFAULT '第 {page} 页'",
    }
    for column, definition in contract_migrations.items():
        if column not in contract_columns:
            connection.execute(text(f"ALTER TABLE contracts ADD COLUMN {column} {definition}"))
    chapter_columns = {column["name"] for column in inspect(connection).get_columns("chapters")}
    chapter_migrations = {"model": "VARCHAR(80) NOT NULL DEFAULT 'qwen3.8-flash'", "description": "TEXT NOT NULL DEFAULT ''",
                          "source_text": "TEXT NOT NULL DEFAULT ''", "reference_text": "TEXT NOT NULL DEFAULT ''",
                          "deleted_at": "DATETIME NULL"}
    for column, definition in chapter_migrations.items():
        if column not in chapter_columns:
            connection.execute(text(f"ALTER TABLE chapters ADD COLUMN {column} {definition}"))
app = FastAPI(title="smartBidding API", version="0.1.0")
app.add_middleware(CORSMiddleware, allow_origins=[x.strip() for x in settings.cors_origins.split(",")], allow_credentials=True, allow_methods=["*"], allow_headers=["*"])
_refreshed_tender_ids: set[int] = set()

def contract_dict(c: Contract):
    try: node_models = json.loads(c.node_models or "{}")
    except (json.JSONDecodeError, TypeError): node_models = {}
    try: final_document = json.loads(c.final_document_json or "") or None
    except (json.JSONDecodeError, TypeError): final_document = None
    return {"id": c.id, "title": c.title, "contract_number": c.contract_number,
            "requirements": c.requirements, "tender_filename": c.tender_filename,
            "tender_source_available": bool(c.tender_layout_filename or c.tender_file_data),
            "tender_source_filename": c.tender_layout_filename or c.tender_filename,
            "tender_compact_text": c.tender_compact_text or compact_document_text(c.tender_text, 30000),
            "reference_filename": c.reference_filename,
            "reference_filenames": [name for name in (c.reference_filename or "").splitlines() if name.strip()],
            "reference_compact_text": c.reference_compact_text or compact_document_text(c.reference_text, 18000), "status": c.status,
            "final_text": c.final_text, "final_check": c.final_check,
            "final_document": final_document, "document_header": c.document_header,
            "document_footer": c.document_footer, "page_number_format": c.page_number_format,
            "node_models": node_models,
            "created_at": c.created_at.isoformat() if c.created_at else None,
            "chapters": [{"id": ch.id, "order": ch.order, "title": ch.title, "knowledge_points": ch.knowledge_points,
                          "description": ch.description, "plan": normalize_model_text(ch.plan), "check_criteria": ch.check_criteria, "prompt": ch.prompt,
                          "source_text": ch.source_text, "reference_text": ch.reference_text,
                          "content": ch.content, "status": ch.status, "self_check": ch.self_check, "model": ch.model}
                         for ch in sorted(c.chapters, key=lambda x: x.order) if ch.deleted_at is None],
            "events": [{"id": event.id, "node_key": event.node_key, "node_name": event.node_name,
                        "status": event.status, "model": event.model, "message": event.message,
                        "created_at": event.created_at.isoformat() if event.created_at else None}
                       for event in sorted(c.events, key=lambda x: x.created_at or 0)]}

@app.get("/api/health")
def health(): return {"ok": True}

@app.post("/api/auth/register", response_model=TokenOut)
def register(data: Credentials, db: Session = Depends(get_db)):
    if db.scalar(select(User).where(User.username == data.username)): raise HTTPException(409, "用户名已存在")
    user = User(username=data.username, password_hash=hash_password(data.password)); db.add(user); db.commit(); db.refresh(user)
    return {"access_token": make_token(user), "username": user.username}

@app.post("/api/auth/login", response_model=TokenOut)
def login(data: Credentials, db: Session = Depends(get_db)):
    user = db.scalar(select(User).where(User.username == data.username))
    if not user or not verify_password(data.password, user.password_hash): raise HTTPException(401, "用户名或密码错误")
    return {"access_token": make_token(user), "username": user.username}

def owned_contract(db, contract_id, user_id):
    c = db.scalar(select(Contract).where(Contract.id == contract_id, Contract.user_id == user_id,
                                        Contract.deleted_at.is_(None)))
    if not c: raise HTTPException(404, "合同项目不存在")
    return c

def owned_contract_any(db, contract_id, user_id):
    c = db.scalar(select(Contract).where(Contract.id == contract_id, Contract.user_id == user_id))
    if not c: raise HTTPException(404, "合同项目不存在")
    return c

def record_event(db: Session, contract_id: int, node_key: str, node_name: str,
                 status: str, message: str, model: str = ""):
    db.add(WorkflowEvent(contract_id=contract_id, node_key=node_key, node_name=node_name,
                         status=status, message=message, model=model))
    db.commit()

def close_streaming_events(db: Session, contract_id: int, message: str):
    events = db.scalars(select(WorkflowEvent).where(
        WorkflowEvent.contract_id == contract_id, WorkflowEvent.status == "streaming")).all()
    for event in events:
        event.status = "failed"
        event.message = f"{event.message} {message}".strip()
    if events:
        db.commit()

def is_current_generation(db: Session, contract_id: int, generation_id: str | None) -> bool:
    if not generation_id:
        return True
    current_id = db.scalar(select(Contract.generation_id).where(Contract.id == contract_id))
    return current_id == generation_id

def gateway_progress_callback(contract_id: int, generation_id: str | None = None):
    def callback(state: str, attempt: int, delay: int, status_code: int,
                 node_key: str = "", phase: str = "", model: str = ""):
        progress_db = SessionLocal()
        try:
            if not is_current_generation(progress_db, contract_id, generation_id):
                return
            if state == "streaming":
                if node_key == "planner":
                    event_key = "planner"
                    event_name = "LangGraph · 需求拆解"
                elif node_key.startswith("chapter_title:"):
                    title = node_key.removeprefix("chapter_title:")
                    chapter = progress_db.scalar(select(Chapter).where(
                        Chapter.contract_id == contract_id, Chapter.title == title,
                        Chapter.deleted_at.is_(None)))
                    event_key = f"chapter:{chapter.id}" if chapter else node_key
                    event_name = f"LangGraph · {chapter.title}" if chapter else f"LangGraph · {title}"
                elif node_key.startswith("stage_design:"):
                    title = node_key.removeprefix("stage_design:")
                    event_key = node_key
                    event_name = f"LangGraph · 新增阶段「{title}」计划"
                else:
                    event_key = node_key or "gateway_stream"
                    event_name = f"LangGraph · {phase or '模型请求'}"
                message = f"接口正在返回{phase or '模型生成'}结果（{model or '当前模型'}，第 {attempt} 次请求）。"
                record_event(progress_db, contract_id, event_key, event_name,
                             "streaming", message, model)
                return
            elif state == "retry":
                reason = f"HTTP {status_code}" if status_code else "接口无响应"
                next_endpoint = "Responses" if attempt % 2 == 1 else "Chat Completions"
                message = f"模型网关第 {attempt} 次请求遇到 {reason}；{delay} 秒后切换到 {next_endpoint} 接口重试（两个接口轮流请求，最多 3 次）。"
                event_status = "running"
            elif state == "recovered":
                message = f"模型网关重试后恢复，已在第 {attempt} 次请求成功。"
                event_status = "completed"
            else:
                reason = f"HTTP {status_code}" if status_code else "接口无响应"
                message = f"模型网关两个接口轮流请求 {attempt} 次后仍失败（{reason}），自动重试次数已用尽。"
                event_status = "failed"
            record_event(progress_db, contract_id, "gateway_retry", "LangGraph · 模型网关重试",
                         event_status, message, model)
        finally:
            progress_db.close()
    return callback

async def extract_document(upload: UploadFile | None) -> tuple[str, str, bytes]:
    if upload is None or not upload.filename:
        return "", "", b""
    filename = Path(upload.filename).name
    payload = await upload.read()
    extension = Path(filename).suffix.lower()
    try:
        if extension in (".txt", ".md", ".text"):
            try: content = payload.decode("utf-8-sig")
            except UnicodeDecodeError: content = payload.decode("gb18030")
        elif extension == ".docx":
            content = extract_docx_text(payload)
        elif extension == ".pdf":
            from pypdf import PdfReader
            reader = PdfReader(BytesIO(payload))
            content = "\n".join(page.extract_text() or "" for page in reader.pages)
        else:
            raise HTTPException(415, f"暂不支持 {extension or '未知'} 格式，请上传 DOCX、PDF、TXT 或 MD 文件")
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(400, f"无法解析文件 {filename}：{str(exc)[:240]}") from exc
    content = content.strip()
    if not content:
        raise HTTPException(400, f"文件 {filename} 没有可提取的文本内容（扫描版 PDF 暂不支持 OCR）")
    return filename, content if extension == ".docx" else content[:120000], payload

def extract_docx_text(document_bytes: bytes) -> str:
    doc = Document(BytesIO(document_bytes))
    blocks = []
    table_index = 0
    from docx.text.paragraph import Paragraph
    from docx.table import Table
    def extract_blocks(parent):
        nonlocal table_index
        for element in parent.iterchildren():
            if element.tag == qn("w:p"):
                paragraph_text = Paragraph(element, doc).text.strip()
                if paragraph_text:
                    blocks.append(paragraph_text)
            elif element.tag == qn("w:tbl"):
                this_table = table_index
                table_index += 1
                table = Table(element, doc)
                blocks.append(f"[[TABLE_{this_table}_START]]")
                for row_index, row in enumerate(table.rows):
                    cells = [cell.text.strip().replace("\n", " / ") for cell in row.cells]
                    blocks.append(f"表格 {this_table} 第 {row_index + 1} 行：" + " | ".join(cells))
                    for cell in row.cells:
                        extract_blocks(cell._tc)
                blocks.append(f"[[TABLE_{this_table}_END]]")
    extract_blocks(doc.element.body)
    return "\n".join(blocks)

def refresh_saved_tender_text(contract: Contract) -> bool:
    if not (contract.tender_file_data and Path(contract.tender_filename).suffix.lower() == ".docx"):
        return False
    if contract.id in _refreshed_tender_ids:
        return False
    refreshed = contract.tender_text or extract_docx_text(contract.tender_file_data)
    if (refreshed.count("_START]]") != refreshed.count("_END]]")
            or "[[TABLE_" not in refreshed):
        refreshed = extract_docx_text(contract.tender_file_data)
    compact = compact_document_text(refreshed, 30000)
    changed = refreshed != contract.tender_text or compact != contract.tender_compact_text
    contract.tender_text = refreshed
    contract.tender_compact_text = compact
    _refreshed_tender_ids.add(contract.id)
    return changed

def extract_docx_cell_text(tc) -> str:
    paragraphs = []
    for paragraph in tc.findall(qn("w:p")):
        parts = []
        for node in paragraph.iter():
            if node.tag == qn("w:t"):
                parts.append(node.text or "")
            elif node.tag == qn("w:tab"):
                parts.append("\t")
            elif node.tag == qn("w:br"):
                parts.append("\n")
        paragraphs.append("".join(parts))
    return "\n".join(paragraphs)

def body_tables(doc):
    return [table for table in doc.element.body.iter(qn("w:tbl"))
            if next(table.iterancestors(qn("w:tbl")), None) is None]

def extract_docx_table_layout(doc) -> list[dict]:
    styles = {style.get(qn("w:styleId")): style for style in doc.styles.element}
    return [docx_table_layout(table, index, styles) for index, table in enumerate(body_tables(doc))]

def docx_table_layout(table, table_index=0, styles=None):
    styles = styles or {}
    table_borders, margins, width, alignment = table_format(table, styles)
    rows = []
    visible_cells = []
    column_count = 0
    prior_grid = {}
    source_cell_index = 0
    for row_index, row in enumerate(table.tr_lst):
        row_cells = []
        next_grid = {}
        column = row.grid_before
        if column:
            row_cells.append({"colspan": column, "placeholder": True})
        for tc in row.tc_lst:
            colspan = tc.grid_span
            anchor = prior_grid.get(column) if tc.vMerge == "continue" else None
            if anchor is not None:
                anchor["rowspan"] += 1
                cell = {"source_cell_index": source_cell_index, "text": "", "original_text": "",
                        "colspan": colspan, "rowspan": 0, "display": False}
            else:
                cell_text = extract_docx_cell_text(tc)
                cell = {"source_cell_index": source_cell_index, "text": cell_text,
                        "original_text": cell_text, "colspan": colspan, "rowspan": 1, "display": True}
                style, borders = cell_format(tc, margins)
                visible_cells.append((cell, row_index, column, style, borders))
                nested = tc.findall(qn("w:tbl"))
                if nested:
                    cell["nested_tables"] = [docx_table_layout(child, i, styles) for i, child in enumerate(nested)]
            row_cells.append(cell)
            if tc.vMerge is not None:
                for col in range(column, column + colspan):
                    next_grid[col] = anchor if anchor is not None else cell
            column += colspan
            source_cell_index += 1
        if row.grid_after:
            row_cells.append({"colspan": row.grid_after, "placeholder": True})
        column_count = max(column_count, column + row.grid_after)
        rows.append(row_cells)
        prior_grid = next_grid
    grid = table.find(qn("w:tblGrid"))
    grid_widths = []
    if grid is not None:
        for column in grid.findall(qn("w:gridCol")):
            value = column.get(qn("w:w"), "")
            grid_widths.append(int(value) if re.fullmatch(r"[0-9]{1,8}", value) else 0)
    column_count = max(column_count, len(grid_widths))
    for cell, row, column, style, borders in visible_cells:
        for edge, logical, inside, outer in (
            ("top", "top", "insideH", row == 0),
            ("bottom", "bottom", "insideH", row + cell["rowspan"] == len(rows)),
            ("left", "start", "insideV", column == 0),
            ("right", "end", "insideV", column + cell["colspan"] == column_count),
        ):
            fallback = table_borders.get(logical, table_borders.get(edge)) if outer else table_borders.get(inside)
            value = borders.get(logical, borders.get(edge, fallback))
            if value is not None:
                style[f"border-{edge}"] = value
        if style:
            cell["word_style"] = style
    return {"table_index": table_index, "grid_columns": column_count, "column_widths": grid_widths,
            "word_width": width, "word_alignment": alignment, "rows": rows}

_TABLE_SEPARATOR = re.compile(r"(?:^|(?<=\s))\|(?=\s|$)")
_TABLE_MARKER = re.compile(r"\[\[TABLE_(\d+)_(START|END)\]\]")
_TABLE_ROW = re.compile(r"表格 (\d+) 第 \d+ 行：(.*)")
_TABLE_HEADER = re.compile(r"序号|编号|行号|项号|条款号")
_TABLE_TOTAL = re.compile(r"(?:总?合计|总计|总分|合计分值)[：:]?")

def tender_text_preview(value: str) -> list[dict]:
    """Build preview blocks without modifying the text stored for planning."""
    blocks = []
    rows = []
    marked = []
    def add_table(table_rows):
        if table_rows:
            blocks.append({"kind": "table", "rows": [
                [{"text": text} for text in row] for row in table_rows
            ]})
    def split_cells(line):
        return [cell.strip() for cell in _TABLE_SEPARATOR.split(line)]

    for line in [*(value or "").splitlines(), ""]:
        marker = _TABLE_MARKER.fullmatch(line.strip())
        if marker:
            table_id, action = marker.groups()
            if action == "START":
                add_table(rows)
                rows = []
                marked.append((table_id, []))
            elif marked and marked[-1][0] == table_id:
                add_table(marked.pop()[1])
            continue
        if marked:
            row = _TABLE_ROW.fullmatch(line)
            if row and row[1] == marked[-1][0]:
                marked[-1][1].append(split_cells(row[2]))
            continue
        if _TABLE_SEPARATOR.search(line):
            cells = split_cells(line)
            if rows:
                # Old text has no table IDs and strips cells that were empty.
                # A header, a finished total, or restarted numbering can expose a boundary.
                last_first = rows[-1][0]
                new_header = len(cells) >= 3 and _TABLE_HEADER.fullmatch(cells[0])
                finished_table = _TABLE_TOTAL.fullmatch(last_first) and not _TABLE_TOTAL.fullmatch(cells[0])
                numbering_restarted = (
                    cells[0] == "1" and last_first.isdecimal() and last_first.lstrip("0") not in ("", "1")
                    and len(cells) >= 3 and len(rows[-1]) >= 3
                )
                if new_header or finished_table or numbering_restarted:
                    add_table(rows)
                    rows = []
            rows.append(cells)
            continue
        add_table(rows)
        rows = []
        if line.strip():
            blocks.append({"kind": "text", "text": line})
    for _, table_rows in reversed(marked):
        add_table(table_rows)
    return blocks

def legacy_tender_tables(value: str) -> list[dict]:
    tables = [block for block in tender_text_preview(value) if block["kind"] == "table"]
    return [{"table_index": index, "rows": table["rows"]} for index, table in enumerate(tables)]

def docx_page_template(doc) -> dict:
    section = doc.sections[0]
    header = section.header.paragraphs[0]
    footer = section.footer.paragraphs[0]
    def text_with_page_field(paragraph):
        parts = []
        skip_result = False
        for node in paragraph._p.iter():
            if node.tag == qn("w:instrText") and "PAGE" in (node.text or "").upper():
                parts.append("{page}")
                skip_result = True
            elif node.tag == qn("w:fldChar"):
                kind = node.get(qn("w:fldCharType"))
                if kind == "end": skip_result = False
            elif node.tag == qn("w:t") and not skip_result:
                parts.append(node.text or "")
            elif node.tag == qn("w:tab"):
                parts.append("\t")
        return "".join(parts)
    page_format = text_with_page_field(footer)
    if "{page}" not in page_format and any("PAGE" in (node.text or "").upper() for node in footer._p.iter() if node.tag == qn("w:instrText")):
        page_format = "第 {page} 页"
    return {"header": text_with_page_field(header), "footer": text_with_page_field(footer),
            "page_number_format": page_format or "第 {page} 页"}

def run_generation(contract_id: int, generation_id: str | None = None):
    db = SessionLocal()
    c = None
    try:
        c = db.get(Contract, contract_id)
        if not c: return
        if generation_id and c.generation_id != generation_id: return
        generation_id = generation_id or c.generation_id
        if not settings.dashscope_api_key:
            raise RuntimeError("未配置 DASHSCOPE_API_KEY，不能使用本地模拟生成")
        refresh_saved_tender_text(c)
        c.status = "planning"
        if not is_current_generation(db, contract_id, generation_id):
            db.rollback()
            return
        db.commit()
        if not c.tender_compact_text and c.tender_text:
            c.tender_compact_text = compact_document_text(c.tender_text, 30000)
        if not c.reference_compact_text and c.reference_text:
            c.reference_compact_text = compact_document_text(c.reference_text, 18000)
        if c.tender_compact_text or c.reference_compact_text:
            db.commit()
        gateway_progress = gateway_progress_callback(contract_id, generation_id)
        try: node_models = json.loads(c.node_models or "{}")
        except (json.JSONDecodeError, TypeError): node_models = {}
        node_models = {"planner": "qwen3.8-flash", "chapter_default": "qwen3.8-flash", **node_models}
        record_event(db, c.id, "tender_parse", "LangGraph · 标书解析", "completed",
                     "已读取合同记录中保存的标书解析文本。" if c.tender_text else "未上传标书，跳过解析。")
        record_event(db, c.id, "reference_parse", "LangGraph · 参考合同解析", "completed",
                     (f"已读取并保存 {len([name for name in c.reference_filename.splitlines() if name.strip()])} 个参考文档的解析文本，用于匹配章节结构与行文风格。"
                      if c.reference_text else "未上传参考合同，跳过解析。"))
        if not is_current_generation(db, contract_id, generation_id):
            return
        chapters = db.scalars(select(Chapter).where(Chapter.contract_id == c.id, Chapter.deleted_at.is_(None)).order_by(Chapter.order)).all()
        saved_generated = []
        if chapters:
            if not any("封面" in chapter.title for chapter in chapters):
                cover = cover_plan(c.title, c.contract_number, c.tender_text, c.reference_text, node_models["chapter_default"])
                cover_chapter = Chapter(contract_id=c.id, order=0, title=cover["title"], description=cover["description"], knowledge_points=cover["knowledge_points"],
                                        plan=cover["plan"], check_criteria=cover["check_criteria"], prompt=cover["prompt"],
                                        source_text=cover["source_text"], reference_text=cover["reference_text"],
                                        model=cover["model"], status="pending")
                db.add(cover_chapter); db.commit(); chapters.insert(0, cover_chapter)
                for index, chapter in enumerate(chapters, start=1): chapter.order = index
                db.commit()
            plans = [{"title": chapter.title, "description": chapter.description, "knowledge_points": chapter.knowledge_points, "plan": normalize_model_text(chapter.plan),
                      "check_criteria": chapter.check_criteria, "prompt": chapter.prompt, "model": chapter.model,
                      "source_text": chapter.source_text, "reference_text": chapter.reference_text} for chapter in chapters]
            plan_count = len(plans)
            c.status = "generating"
            for chapter in chapters:
                if chapter.content and chapter.self_check:
                    chapter.status = "completed"
                    saved_generated.append({"title": chapter.title, "content": chapter.content,
                                            "self_check": chapter.self_check, "model": chapter.model})
                else:
                    chapter.status = "generating"
            if not is_current_generation(db, contract_id, generation_id):
                db.rollback()
                return
            db.commit()
            record_event(db, c.id, "planner", "LangGraph · 阶段计划", "completed",
                         f"沿用已维护的 {plan_count} 个阶段及当前排序，不重新拆分。", node_models["planner"])
            remaining_count = plan_count - len(saved_generated)
            record_event(db, c.id, "dispatch", "LangGraph · Send 并行分发", "completed",
                         f"按当前阶段顺序分发 {remaining_count} 个未完成节点；已完成节点将沿用现有结果。")
            for chapter in chapters:
                if chapter.status != "completed":
                    record_event(db, c.id, f"chapter:{chapter.id}", f"LangGraph · {chapter.title}", "running",
                                 "章节生成和自查节点开始执行。", chapter.model)
        else:
            record_event(db, c.id, "planner", "LangGraph · 需求拆解", "running",
                         ("正在理解标书与用户要求并拆分阶段；参考文档未上传，已跳过参考匹配。"
                          if not c.reference_text else
                          "正在理解标书与用户要求并拆分阶段，同时匹配参考文档中的章节结构与行文风格。"),
                         node_models["planner"])
            plans = None
            plan_count = 0
        state = {"requirements": c.requirements, "node_models": node_models,
                 "contract_title": c.title, "contract_number": c.contract_number,
                 "tender_text": c.tender_text, "reference_text": c.reference_text,
                 "tender_compact_text": c.tender_compact_text, "reference_compact_text": c.reference_compact_text,
                 "retry_callback": gateway_progress}
        if plans is not None:
            state["plans"] = plans
            state["generated"] = saved_generated
            state["completed_titles"] = [item["title"] for item in saved_generated]
        completed_chapters = len(saved_generated)
        merge_started = bool(plan_count and completed_chapters == plan_count)
        if merge_started:
            record_event(db, c.id, "merge", "LangGraph · 全文合并", "running",
                         "所有阶段已有可用的正文和自查结果；按阶段顺序直接拼接全文。")
        for update in workflow.stream(state, stream_mode="updates"):
            if not is_current_generation(db, contract_id, generation_id):
                return
            if "plan" in update:
                plans = update["plan"]["plans"]
                plan_count = len(plans)
                existing = {ch.title: ch for ch in db.scalars(select(Chapter).where(Chapter.contract_id == c.id, Chapter.deleted_at.is_(None))).all()}
                for i, p in enumerate(plans):
                    ch = existing.get(p["title"])
                    if ch:
                        ch.order = i+1; ch.knowledge_points = p.get("knowledge_points", ""); ch.plan = p.get("plan", "")
                        ch.description = p.get("description", ch.description); ch.check_criteria = p.get("check_criteria", ""); ch.prompt = p.get("prompt", ch.prompt)
                        ch.source_text = p.get("source_text", ""); ch.reference_text = p.get("reference_text", "")
                        ch.model = p.get("model", node_models["chapter_default"]); ch.status = "pending"
                    else:
                        db.add(Chapter(contract_id=c.id, order=i+1, title=p["title"], description=p.get("description", p.get("plan", "")),
                                       knowledge_points=p.get("knowledge_points", ""),
                                       plan=p.get("plan", ""), check_criteria=p.get("check_criteria", ""), prompt=p.get("prompt", ""),
                                       source_text=p.get("source_text", ""), reference_text=p.get("reference_text", ""),
                                       model=p.get("model", node_models["chapter_default"]), status="pending"))
                c.status = "generating"
                if not is_current_generation(db, contract_id, generation_id):
                    db.rollback()
                    return
                db.commit()
                record_event(db, c.id, "planner", "LangGraph · 需求拆解", "completed",
                             f"规划完成，生成 {plan_count} 个章节节点；下游将使用 LangGraph Send 并行分发。", node_models["planner"])
                record_event(db, c.id, "dispatch", "LangGraph · Send 并行分发", "completed",
                             f"已将 {plan_count} 个章节任务分发到独立生成与自查节点。")
                for p in plans:
                    ch = db.scalar(select(Chapter).where(Chapter.contract_id == c.id, Chapter.deleted_at.is_(None), Chapter.title == p["title"]))
                    if ch:
                        ch.status = "generating"; db.commit()
                        record_event(db, c.id, f"chapter:{ch.id}", f"LangGraph · {ch.title}", "running",
                                     "章节生成和自查节点开始执行。", ch.model)
            if "generate" in update:
                for item in update["generate"].get("generated", []):
                    if not is_current_generation(db, contract_id, generation_id):
                        return
                    ch = db.scalar(select(Chapter).where(Chapter.contract_id == c.id, Chapter.deleted_at.is_(None), Chapter.title == item["title"]))
                    if ch:
                        ch.content = item["content"]; ch.self_check = item["self_check"]; ch.status = "completed"
                        vn = db.scalar(select(func.count(Version.id)).where(Version.chapter_id == ch.id)) or 0
                        db.add(Version(contract_id=c.id, chapter_id=ch.id, version_no=vn+1, content=ch.content))
                        if not is_current_generation(db, contract_id, generation_id):
                            db.rollback()
                            return
                        db.commit()
                        completed_chapters += 1
                        record_event(db, c.id, f"chapter:{ch.id}", f"LangGraph · {ch.title}", "completed",
                                     "章节正文已生成，节点自查已完成并保存版本。", item.get("model", ch.model))
                if plan_count and completed_chapters == plan_count and not merge_started:
                    merge_started = True
                    record_event(db, c.id, "merge", "LangGraph · 全文合并", "running",
                                 "所有章节生成与自查均已完成；LangGraph 正在按阶段顺序直接拼接全文。")
            if "merge" in update:
                c.final_text = update["merge"]["final_text"]
                c.final_check = ""
                c.final_document_json = ""
                c.status = "completed"
                vn = db.scalar(select(func.count(Version.id)).where(Version.contract_id == c.id, Version.chapter_id.is_(None))) or 0
                db.add(Version(contract_id=c.id, chapter_id=None, version_no=vn+1, content=c.final_text))
                if not is_current_generation(db, contract_id, generation_id):
                    db.rollback()
                    return
                db.commit()
                record_event(db, c.id, "merge", "LangGraph · 全文合并", "completed",
                             "完整合同已按阶段顺序直接拼接并保存版本；流程结束。")
    except Exception as exc:
        c = db.get(Contract, contract_id)
        if c and not is_current_generation(db, contract_id, generation_id):
            return
        if c:
            c.status = "failed"; c.final_check = ""; db.commit()
            close_streaming_events(db, c.id, "流式请求未完成，工作流已失败。")
            record_event(db, c.id, "workflow", "LangGraph · 流程异常", "failed", str(exc)[:800])
    finally: db.close()

@app.get("/api/contracts")
def list_contracts(db: Session = Depends(get_db), user: User = Depends(current_user)):
    items = db.scalars(select(Contract).where(Contract.user_id == user.id, Contract.deleted_at.is_(None)).order_by(Contract.created_at.desc())).all()
    return [{"id": c.id, "title": c.title, "contract_number": c.contract_number, "status": c.status,
             "created_at": c.created_at.isoformat(), "chapter_count": sum(ch.deleted_at is None for ch in c.chapters)} for c in items]

@app.post("/api/contracts", status_code=202)
async def create_contract(tasks: BackgroundTasks, title: str = Form(...), contract_number: str = Form(...),
                          requirements: str = Form(default=""), planner_model: ModelName = Form(default="qwen3.8-flash"),
                          chapter_model: ModelName = Form(default="qwen3.8-flash"),
                          tender_file: UploadFile | None = File(default=None),
                          reference_files: list[UploadFile] | None = File(default=None),
                          reference_file: UploadFile | None = File(default=None),
                          db: Session = Depends(get_db),
                          user: User = Depends(current_user)):
    if not title.strip() or not contract_number.strip(): raise HTTPException(422, "合同名称和合同编号不能为空")
    if len(title.strip()) > 200 or len(contract_number.strip()) > 120 or len(requirements) > 20000:
        raise HTTPException(422, "合同名称、编号或补充要求超过长度限制")
    tender_filename, tender_text, tender_file_data = await extract_document(tender_file)
    reference_uploads = [item for item in (reference_files or []) if item and item.filename]
    if reference_file and reference_file.filename and all(item.filename != reference_file.filename for item in reference_uploads):
        reference_uploads.append(reference_file)
    reference_documents = []
    for upload in reference_uploads:
        filename, content, _ = await extract_document(upload)
        reference_documents.append((filename, content))
    reference_filename = "\n".join(filename for filename, _ in reference_documents)
    reference_text = "\n\n".join(f"【参考文档：{filename}】\n{content}" for filename, content in reference_documents)
    tender_compact_text = compact_document_text(tender_text, 30000)
    if reference_documents:
        markers = [f"【参考文档：{filename}】" for filename, _ in reference_documents]
        compact_budget = max(1, (18000 - sum(len(marker) + 2 for marker in markers)) // len(reference_documents))
        reference_compact_text = "\n".join(
            f"{marker}\n{compact_document_text(content, compact_budget)}"
            for marker, (_, content) in zip(markers, reference_documents))[:18000]
    else:
        reference_compact_text = ""
    if len(requirements.strip()) < 10 and not tender_text:
        raise HTTPException(422, "请填写至少 10 个字的合同要求，或上传标书作为拆分依据")
    node_models = {"planner": planner_model, "chapter_default": chapter_model}
    c = Contract(user_id=user.id, title=title.strip(), contract_number=contract_number.strip(),
                 requirements=requirements.strip(), tender_filename=tender_filename, tender_text=tender_text,
                 tender_compact_text=tender_compact_text, tender_file_data=tender_file_data,
                 reference_filename=reference_filename, reference_text=reference_text,
                 reference_compact_text=reference_compact_text, status="queued",
                 generation_id=str(uuid4()), node_models=json.dumps(node_models)); db.add(c); db.commit(); db.refresh(c)
    tasks.add_task(run_generation, c.id, c.generation_id)
    return {"id": c.id, "status": c.status}

@app.get("/api/contracts/{contract_id}")
def get_contract(contract_id: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
    contract = owned_contract(db, contract_id, user.id)
    if refresh_saved_tender_text(contract):
        db.commit()
    return contract_dict(contract)

@app.get("/api/contracts/{contract_id}/tender-source")
def get_tender_source(contract_id: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
    c = owned_contract(db, contract_id, user.id)
    payload = c.tender_layout_data or c.tender_file_data
    if not payload:
        raise HTTPException(404, "该项目未保存原始标书文件，请重新上传原 DOCX 后查看。")
    filename = Path(c.tender_layout_filename or c.tender_filename).name or f"tender-{c.id}.docx"
    media_types = {".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                   ".pdf": "application/pdf", ".txt": "text/plain; charset=utf-8",
                   ".md": "text/markdown; charset=utf-8", ".text": "text/plain; charset=utf-8"}
    disposition = "attachment" if Path(filename).suffix.lower() == ".docx" else "inline"
    return StreamingResponse(BytesIO(payload), media_type=media_types.get(Path(filename).suffix.lower(), "application/octet-stream"),
                             headers={"Content-Disposition": f"{disposition}; filename*=UTF-8''{quote(filename)}"})

@app.put("/api/contracts/{contract_id}/tender-source")
async def restore_tender_source(contract_id: int, tender_file: UploadFile = File(...),
                                db: Session = Depends(get_db), user: User = Depends(current_user)):
    c = owned_contract(db, contract_id, user.id)
    filename = Path(tender_file.filename or "").name
    if Path(filename).suffix.lower() != ".docx":
        raise HTTPException(415, "请上传原始 DOCX 标书，以便恢复 Word 表格样式。")
    payload = await tender_file.read()
    try:
        layout = read_tender_layout(filename, payload)
    except Exception as exc:
        raise HTTPException(422, "无法读取这个 DOCX 文件，请选择有效的原始标书。") from exc
    c.tender_layout_filename = filename
    c.tender_layout_data = payload
    db.commit()
    return layout

def tender_docx_source(contract: Contract):
    if contract.tender_layout_filename and contract.tender_layout_data:
        return contract.tender_layout_filename, contract.tender_layout_data
    if Path(contract.tender_filename).suffix.lower() == ".docx" and contract.tender_file_data:
        return contract.tender_filename, contract.tender_file_data
    return "", b""

def read_tender_layout(filename, payload):
    doc = Document(BytesIO(payload))
    return {"supported": True, "filename": filename, "revision": sha256(payload).hexdigest(),
            **docx_page_template(doc), "tables": extract_docx_table_layout(doc)}

@app.get("/api/contracts/{contract_id}/tender-layout")
def get_tender_layout(contract_id: int, split_preview: bool = False, db: Session = Depends(get_db), user: User = Depends(current_user)):
    c = owned_contract(db, contract_id, user.id)
    filename, payload = tender_docx_source(c)
    preview = {}
    if split_preview:
        split_text = c.tender_compact_text or compact_document_text(c.tender_text, 30000)
        preview["split_blocks"] = tender_text_preview(split_text)
    if not payload:
        tables = legacy_tender_tables(c.tender_text or c.tender_compact_text) if c.tender_filename.lower().endswith(".docx") else []
        reason = "旧项目未保存原始 DOCX。请在当前项目补传原标书，以恢复原表格及合并单元格。"
        if not c.tender_filename:
            reason = "当前项目未上传标书，可补传 DOCX 以预览原始表格。"
        elif not c.tender_filename.lower().endswith(".docx"):
            reason = "当前标书格式无法恢复 Word 表格结构，可补传对应的原始 DOCX。"
        return {"supported": False, "reason": reason, "tables": tables, **preview}
    try:
        return {**read_tender_layout(filename, payload), **preview}
    except Exception as exc:
        raise HTTPException(422, "无法读取已保存的 DOCX，请补传有效的原始标书。") from exc

@app.delete("/api/contracts/{contract_id}", status_code=202)
def delete_contract(contract_id: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
    c = owned_contract(db, contract_id, user.id)
    c.deleted_at = datetime.now(timezone.utc)
    c.generation_id = str(uuid4())
    if c.status in ("queued", "planning", "generating"):
        c.status = "pending"
        for chapter in c.chapters:
            if chapter.deleted_at is None and chapter.status == "generating":
                chapter.status = "pending"
    db.commit()
    record_event(db, c.id, "project_trash", "项目回收站", "completed", "项目已移入回收站，可从回收站恢复。")
    return {"status": "trashed"}

@app.delete("/api/contracts/{contract_id}/chapters/{chapter_id}", status_code=202)
def delete_chapter(contract_id: int, chapter_id: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
    c = owned_contract(db, contract_id, user.id)
    if c.status in ("queued", "planning", "generating"):
        raise HTTPException(409, "生成过程中不能删除阶段")
    chapter = db.scalar(select(Chapter).where(Chapter.id == chapter_id, Chapter.contract_id == c.id,
                                         Chapter.deleted_at.is_(None)))
    if not chapter:
        raise HTTPException(404, "阶段不存在")
    chapter.deleted_at = datetime.now(timezone.utc)
    for order, active in enumerate(db.scalars(select(Chapter).where(
            Chapter.contract_id == c.id, Chapter.deleted_at.is_(None), Chapter.id != chapter.id
    ).order_by(Chapter.order)).all(), start=1):
        active.order = order
    c.generation_id = str(uuid4())
    c.final_text = ""; c.final_document_json = ""; c.final_check = ""; c.status = "pending"
    db.commit()
    record_event(db, c.id, f"chapter_trash:{chapter.id}", f"回收站 · {chapter.title}", "completed", "阶段已移入回收站，可恢复。")
    return {"status": "trashed"}

@app.get("/api/recycle-bin")
def get_recycle_bin(db: Session = Depends(get_db), user: User = Depends(current_user)):
    deleted_contracts = db.scalars(select(Contract).where(
        Contract.user_id == user.id, Contract.deleted_at.is_not(None)
    ).order_by(Contract.deleted_at.desc())).all()
    deleted_chapters = db.execute(select(Chapter, Contract).join(Contract).where(
        Contract.user_id == user.id, Contract.deleted_at.is_(None), Chapter.deleted_at.is_not(None)
    ).order_by(Chapter.deleted_at.desc())).all()
    return {
        "projects": [{"id": c.id, "title": c.title, "contract_number": c.contract_number,
                      "deleted_at": c.deleted_at.isoformat() if c.deleted_at else None} for c in deleted_contracts],
        "chapters": [{"id": ch.id, "contract_id": c.id, "contract_title": c.title, "title": ch.title,
                       "deleted_at": ch.deleted_at.isoformat() if ch.deleted_at else None} for ch, c in deleted_chapters],
    }

@app.post("/api/recycle-bin/contracts/{contract_id}/restore")
def restore_contract(contract_id: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
    c = owned_contract_any(db, contract_id, user.id)
    if c.deleted_at is None:
        raise HTTPException(409, "项目不在回收站中")
    c.deleted_at = None
    c.generation_id = str(uuid4())
    db.commit()
    record_event(db, c.id, "project_restore", "项目回收站", "completed", "项目已从回收站恢复。")
    return {"status": "restored"}

@app.post("/api/recycle-bin/contracts/{contract_id}/chapters/{chapter_id}/restore")
def restore_chapter(contract_id: int, chapter_id: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
    c = owned_contract(db, contract_id, user.id)
    chapter = db.scalar(select(Chapter).where(Chapter.id == chapter_id, Chapter.contract_id == c.id,
                                         Chapter.deleted_at.is_not(None)))
    if not chapter:
        raise HTTPException(404, "回收站中找不到该阶段")
    chapter.deleted_at = None
    chapter.order = (db.scalar(select(func.max(Chapter.order)).where(
        Chapter.contract_id == c.id, Chapter.deleted_at.is_(None), Chapter.id != chapter.id
    )) or 0) + 1
    chapter.status = "completed" if chapter.content else "pending"
    c.generation_id = str(uuid4())
    c.final_text = ""; c.final_document_json = ""; c.final_check = ""; c.status = "pending"
    db.commit()
    record_event(db, c.id, f"chapter_restore:{chapter.id}", f"回收站 · {chapter.title}", "completed", "阶段已恢复并添加到阶段列表末尾。")
    return {"status": "restored"}

@app.post("/api/contracts/{contract_id}/chapters", status_code=201)
def create_chapter(contract_id: int, data: ChapterCreate, db: Session = Depends(get_db), user: User = Depends(current_user)):
    c = owned_contract(db, contract_id, user.id)
    generation_id = c.generation_id
    if c.status in ("planning", "generating"): raise HTTPException(409, "生成过程中不能新增阶段")
    if db.scalar(select(Chapter.id).where(Chapter.contract_id == c.id, Chapter.deleted_at.is_(None), Chapter.title == data.title.strip())):
        raise HTTPException(409, "该阶段名称已存在")
    try: node_models = json.loads(c.node_models or "{}")
    except (json.JSONDecodeError, TypeError): node_models = {}
    model = node_models.get("chapter_default", "qwen3.8-flash")
    try:
        design = draft_stage(data.title.strip(), data.description.strip(), c.title, c.contract_number,
                             c.requirements, c.tender_text, c.reference_text, model,
                             gateway_progress_callback(c.id, generation_id))
    except Exception as exc:
        record_event(db, c.id, f"stage_design:{data.title.strip()}",
                     f"LangGraph · 新增阶段「{data.title.strip()}」计划", "failed",
                     f"阶段计划生成失败：{str(exc)[:600]}", model)
        raise HTTPException(502, f"阶段自动规划失败：{str(exc)[:500]}") from exc
    if not is_current_generation(db, c.id, generation_id):
        raise HTTPException(409, "全流程已重启，本次新增阶段已取消")
    existing = db.scalars(select(Chapter).where(Chapter.contract_id == c.id, Chapter.deleted_at.is_(None)).order_by(Chapter.order)).all()
    if not any("封面" in chapter.title for chapter in existing):
        cover = cover_plan(c.title, c.contract_number, c.tender_text, c.reference_text,
                           model)
        db.add(Chapter(contract_id=c.id, order=1, title=cover["title"], description=cover["description"], knowledge_points=cover["knowledge_points"],
                       plan=cover["plan"], check_criteria=cover["check_criteria"], prompt=cover["prompt"],
                       source_text=cover["source_text"], reference_text=cover["reference_text"],
                       model=cover["model"], status="pending"))
        for chapter in existing: chapter.order += 1
        db.flush()
    order = (db.scalar(select(func.max(Chapter.order)).where(Chapter.contract_id == c.id)) or 0) + 1
    chapter = Chapter(contract_id=c.id, order=order, title=data.title.strip(), description=design["description"],
                       knowledge_points=design["knowledge_points"], plan=design["plan"],
                       check_criteria=design["check_criteria"], prompt=design["prompt"],
                       source_text=design["source_text"], reference_text=design["reference_text"],
                       model=model, status="pending")
    c.final_text = ""; c.final_document_json = ""; c.final_check = ""; c.status = "pending"
    db.add(chapter); db.commit(); db.refresh(chapter)
    record_event(db, c.id, f"chapter:{chapter.id}", f"LangGraph · {chapter.title}", "completed", "模型已自动生成阶段计划、检查标准和提示词；章节正文将在全文生成时起草。", chapter.model)
    return {"id": chapter.id, "order": chapter.order, "title": chapter.title, "description": chapter.description,
            "knowledge_points": chapter.knowledge_points,
            "plan": chapter.plan, "check_criteria": chapter.check_criteria, "prompt": chapter.prompt,
            "source_text": chapter.source_text, "reference_text": chapter.reference_text,
            "model": chapter.model, "status": chapter.status, "content": chapter.content, "self_check": chapter.self_check}

@app.put("/api/contracts/{contract_id}/chapters/order")
def reorder_chapters(contract_id: int, data: ChapterOrderUpdate, db: Session = Depends(get_db), user: User = Depends(current_user)):
    c = owned_contract(db, contract_id, user.id)
    if c.status in ("planning", "generating"): raise HTTPException(409, "生成过程中不能调整阶段顺序")
    chapters = db.scalars(select(Chapter).where(Chapter.contract_id == c.id, Chapter.deleted_at.is_(None))).all()
    actual_ids = {chapter.id for chapter in chapters}
    if len(data.chapter_ids) != len(actual_ids) or set(data.chapter_ids) != actual_ids:
        raise HTTPException(400, "阶段列表已变化，请刷新页面后再排序")
    by_id = {chapter.id: chapter for chapter in chapters}
    for position, chapter_id in enumerate(data.chapter_ids, start=1):
        by_id[chapter_id].order = position
    c.final_text = ""; c.final_document_json = ""; c.final_check = ""; c.status = "pending"
    db.commit()
    record_event(db, c.id, "stage_order", "LangGraph · 阶段排序", "completed", "用户调整了阶段顺序；下一次全文生成将采用新顺序。")
    return {"chapter_ids": data.chapter_ids}

@app.patch("/api/contracts/{contract_id}/chapters/{chapter_id}/title")
def update_chapter_title(contract_id: int, chapter_id: int, data: ChapterTitleUpdate,
                         db: Session = Depends(get_db), user: User = Depends(current_user)):
    c = owned_contract(db, contract_id, user.id)
    if c.status in ("planning", "generating"):
        raise HTTPException(409, "生成过程中不能修改阶段标题")
    chapter = db.scalar(select(Chapter).where(Chapter.id == chapter_id, Chapter.contract_id == c.id,
                                               Chapter.deleted_at.is_(None)))
    if not chapter:
        raise HTTPException(404, "阶段不存在")
    title = data.title.strip()
    if not title:
        raise HTTPException(422, "阶段标题不能为空")
    duplicate = db.scalar(select(Chapter.id).where(Chapter.contract_id == c.id,
        Chapter.deleted_at.is_(None), Chapter.id != chapter.id, Chapter.title == title))
    if duplicate:
        raise HTTPException(409, "该阶段名称已存在")
    if "封面" in chapter.title and "封面" not in title:
        raise HTTPException(409, "默认封面阶段必须保留“封面”字样")
    if "封面" in title and chapter.order != 1:
        raise HTTPException(409, "封面阶段必须排在第一位")
    previous_title = chapter.title
    chapter.title = title
    c.final_text = ""
    c.final_document_json = ""
    c.final_check = ""
    if c.status == "completed":
        c.status = "pending"
    db.commit()
    record_event(db, c.id, f"chapter:{chapter.id}", f"LangGraph · {title}", "completed",
                 f"阶段标题从“{previous_title}”更新为“{title}”；原阶段正文与历史版本已保留。", chapter.model)
    return {"id": chapter.id, "title": chapter.title}

@app.post("/api/contracts/{contract_id}/generate", status_code=202)
def generate_again(contract_id: int, tasks: BackgroundTasks, db: Session = Depends(get_db), user: User = Depends(current_user)):
    c = owned_contract(db, contract_id, user.id)
    if c.status in ("planning", "generating"): raise HTTPException(409, "该项目正在生成中")
    c.status = "queued"; c.final_text = ""; c.final_document_json = ""; c.final_check = ""; c.generation_id = str(uuid4())
    for chapter in c.chapters:
        if chapter.deleted_at is not None: continue
        chapter.status = "pending"; chapter.content = ""; chapter.self_check = ""
    db.commit(); tasks.add_task(run_generation, c.id, c.generation_id); return {"status": "queued"}

@app.post("/api/contracts/{contract_id}/restart", status_code=202)
def restart_all(contract_id: int, tasks: BackgroundTasks, db: Session = Depends(get_db), user: User = Depends(current_user)):
    c = owned_contract(db, contract_id, user.id)
    c.generation_id = str(uuid4())
    c.status = "queued"; c.final_text = ""; c.final_document_json = ""; c.final_check = ""
    for chapter in c.chapters:
        if chapter.deleted_at is not None: continue
        chapter.status = "pending"; chapter.content = ""; chapter.self_check = ""
    db.commit()
    record_event(db, c.id, "workflow_restart", "LangGraph · 全流程重启", "completed",
                 "用户重启了全部流程；旧流程结果将被忽略，按当前阶段列表重新生成全部章节并执行全文合并。")
    tasks.add_task(run_generation, c.id, c.generation_id)
    return {"status": "queued"}

@app.post("/api/contracts/{contract_id}/stop", status_code=202)
def stop_workflow(contract_id: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
    c = owned_contract(db, contract_id, user.id)
    if c.status not in ("queued", "planning", "generating"):
        raise HTTPException(409, "当前没有可停止的推理流程")
    c.generation_id = str(uuid4())
    c.status = "paused"
    for chapter in c.chapters:
        if chapter.deleted_at is None and chapter.status == "generating":
            chapter.status = "pending"
    db.commit()
    record_event(db, c.id, "workflow_stop", "LangGraph · 流程已停止", "completed",
                 "当前运行已停止；已完成章节及历史版本保留，未完成节点可继续执行。")
    return {"status": "paused"}

@app.post("/api/contracts/{contract_id}/continue", status_code=202)
def continue_workflow(contract_id: int, tasks: BackgroundTasks, db: Session = Depends(get_db), user: User = Depends(current_user)):
    c = owned_contract(db, contract_id, user.id)
    if c.status in ("queued", "planning", "generating"):
        raise HTTPException(409, "流程仍在执行中")
    if c.status not in ("paused", "pending", "failed", "completed"):
        raise HTTPException(409, "当前状态不能继续生成")
    chapters = db.scalars(select(Chapter).where(Chapter.contract_id == c.id, Chapter.deleted_at.is_(None))).all()
    if not chapters and c.status not in ("paused", "failed"):
        raise HTTPException(409, "没有可继续的阶段节点")
    c.generation_id = str(uuid4())
    c.status = "queued"
    c.final_text = ""
    c.final_document_json = ""
    c.final_check = ""
    for chapter in chapters:
        chapter.status = "completed" if chapter.content and chapter.self_check else "pending"
    db.commit()
    record_event(db, c.id, "workflow_continue", "LangGraph · 继续流程", "completed",
                 "继续生成已启动；保留已完成章节，仅重跑未完成节点，然后执行全文合并。")
    tasks.add_task(run_generation, c.id, c.generation_id)
    return {"status": "queued"}

@app.post("/api/contracts/{contract_id}/merge-now")
def merge_now(contract_id: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
    c = owned_contract(db, contract_id, user.id)
    generation_id = c.generation_id
    chapters = db.scalars(select(Chapter).where(Chapter.contract_id == c.id, Chapter.deleted_at.is_(None)).order_by(Chapter.order)).all()
    if not chapters:
        raise HTTPException(409, "尚无阶段计划，暂时无法合并文本")
    state = {
        "plans": [{"title": chapter.title} for chapter in chapters],
        "generated": [{"title": chapter.title,
                       "content": chapter.content or "【该阶段尚未生成，生成完成后可再次合并】"}
                      for chapter in chapters],
    }
    record_event(db, c.id, "merge_now", "LangGraph · 直接合并文本", "running",
                 "按当前阶段顺序直接拼接已有章节文本；尚未生成的阶段会保留待生成标记。")
    try:
        merged = merge_node(state)["final_text"]
        if not is_current_generation(db, c.id, generation_id):
            raise HTTPException(409, "全流程已重启，旧的合并结果已丢弃")
    except HTTPException:
        raise
    except Exception as exc:
        record_event(db, c.id, "merge_now", "LangGraph · 直接合并文本", "failed", str(exc)[:800])
        raise HTTPException(502, f"直接生成文本失败：{str(exc)[:500]}") from exc
    c.final_text = merged
    c.final_check = ""
    c.final_document_json = ""
    if all(chapter.content and chapter.self_check for chapter in chapters):
        c.status = "completed"
    version_no = db.scalar(select(func.count(Version.id)).where(Version.contract_id == c.id, Version.chapter_id.is_(None))) or 0
    db.add(Version(contract_id=c.id, chapter_id=None, version_no=version_no + 1, content=merged))
    db.commit()
    record_event(db, c.id, "merge_now", "LangGraph · 直接合并文本", "completed",
                 "已基于当前可用章节文本直接完成拼接；未完成阶段保留待生成标记。")
    return {"final_text": merged}

@app.patch("/api/contracts/{contract_id}/chapters/{chapter_id}/prompt")
def update_prompt(contract_id: int, chapter_id: int, data: PromptUpdate, db: Session = Depends(get_db), user: User = Depends(current_user)):
    c = owned_contract(db, contract_id, user.id); ch = db.scalar(select(Chapter).where(Chapter.id == chapter_id, Chapter.contract_id == c.id, Chapter.deleted_at.is_(None)))
    if not ch: raise HTTPException(404, "章节不存在")
    ch.prompt = data.prompt; db.commit(); return {"ok": True}

@app.put("/api/contracts/{contract_id}/final-document")
def save_final_document(contract_id: int, data: FinalDocumentUpdate,
                        db: Session = Depends(get_db), user: User = Depends(current_user)):
    c = owned_contract(db, contract_id, user.id)
    total_chars = sum(len(block.text) + sum(len(cell.text) for row in block.rows for cell in row)
                      for section in data.sections for block in section.blocks)
    if total_chars > 500000:
        raise HTTPException(413, "编辑文本超过 500000 字符")
    document = data.model_dump(mode="json")
    c.final_document_json = json.dumps(document, ensure_ascii=False)
    c.document_header = data.header
    c.document_footer = data.footer
    c.page_number_format = data.page_number_format
    sections = []
    for section in data.sections:
        if section.source_material:
            continue
        blocks = []
        for block in section.blocks:
            if block.kind != "table":
                if block.text.strip():
                    blocks.append(block.text)
                continue
            rows = ["| " + " | ".join(cell.text.replace("|", r"\|").replace("\n", " ") for cell in row) + " |"
                    for row in block.rows]
            if rows and block.header_row:
                alignment = {"left": ":---", "center": ":---:", "right": "---:"}
                columns = [alignment.get(item, "---") for item in block.column_alignments]
                header_width = len(block.rows[0])
                columns = (columns + ["---"] * header_width)[:header_width]
                rows.insert(1, "| " + " | ".join(columns) + " |")
            if rows:
                blocks.append("\n".join(rows))
        sections.append("\n\n".join([section.title, *blocks]))
    c.final_text = "\n\n".join(sections)
    db.commit()
    return {"ok": True, "saved_at": datetime.now(timezone.utc).isoformat(), "final_document": document,
            "final_text": c.final_text}

@app.patch("/api/contracts/{contract_id}/models")
def update_stage_model(contract_id: int, data: StageModelUpdate, db: Session = Depends(get_db), user: User = Depends(current_user)):
    c = owned_contract(db, contract_id, user.id)
    try: models = json.loads(c.node_models or "{}")
    except (json.JSONDecodeError, TypeError): models = {}
    models[data.stage] = data.model; c.node_models = json.dumps(models); db.commit()
    return {"node_models": models}

@app.patch("/api/contracts/{contract_id}/chapters/{chapter_id}/model")
def update_chapter_model(contract_id: int, chapter_id: int, data: ModelUpdate, db: Session = Depends(get_db), user: User = Depends(current_user)):
    c = owned_contract(db, contract_id, user.id)
    ch = db.scalar(select(Chapter).where(Chapter.id == chapter_id, Chapter.contract_id == c.id, Chapter.deleted_at.is_(None)))
    if not ch: raise HTTPException(404, "章节不存在")
    ch.model = data.model; db.commit()
    return {"model": ch.model}

@app.post("/api/contracts/{contract_id}/chapters/{chapter_id}/generate", status_code=202)
def generate_chapter(contract_id: int, chapter_id: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
    c = owned_contract(db, contract_id, user.id); ch = db.scalar(select(Chapter).where(Chapter.id == chapter_id, Chapter.contract_id == c.id, Chapter.deleted_at.is_(None)))
    if not ch: raise HTTPException(404, "章节不存在")
    c.generation_id = str(uuid4())
    generation_id = c.generation_id
    c.status = "paused"
    c.final_text = ""
    c.final_document_json = ""
    c.final_check = ""
    for chapter in c.chapters:
        if chapter.deleted_at is None and chapter.status == "generating":
            chapter.status = "pending"
    from .generator import chapter_workflow
    record_event(db, c.id, f"chapter:{ch.id}", f"LangGraph · {ch.title}", "running", "手动重试该阶段；原流程已暂停，其他未完成节点可在本阶段完成后继续生成。", ch.model)
    ch.status = "generating"; db.commit()
    try:
        result = chapter_workflow.invoke({"requirements": c.requirements, "chapter": {
            "title": ch.title, "description": ch.description, "plan": normalize_model_text(ch.plan), "knowledge_points": ch.knowledge_points,
            "check_criteria": ch.check_criteria, "prompt": ch.prompt, "model": ch.model,
            "source_text": ch.source_text, "reference_text": ch.reference_text,
        }, "contract_title": c.title, "contract_number": c.contract_number,
            "retry_callback": gateway_progress_callback(c.id, generation_id)})
        generated = result["generated"][0]
        content, check = generated["content"], generated["self_check"]
    except Exception as exc:
        if not is_current_generation(db, c.id, generation_id):
            db.rollback()
            raise HTTPException(409, "全流程已重启，旧的章节结果已丢弃") from exc
        ch.status = "failed"; db.commit()
        record_event(db, c.id, f"chapter:{ch.id}", f"LangGraph · {ch.title}", "failed", str(exc)[:800], ch.model)
        raise HTTPException(502, str(exc))
    if not is_current_generation(db, c.id, generation_id):
        raise HTTPException(409, "全流程已重启，旧的章节结果已丢弃")
    ch.content = content; ch.self_check = check; ch.status = "completed"
    vn = db.scalar(select(func.count(Version.id)).where(Version.chapter_id == ch.id)) or 0
    db.add(Version(contract_id=c.id, chapter_id=ch.id, version_no=vn+1, content=content)); db.commit()
    record_event(db, c.id, f"chapter:{ch.id}", f"LangGraph · {ch.title}", "completed", "手动生成已完成，章节自查通过模型执行并保存新版本。", ch.model)
    return {"content": content, "self_check": check}

@app.get("/api/contracts/{contract_id}/versions")
def versions(contract_id: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
    c = owned_contract(db, contract_id, user.id)
    return [{"id": v.id, "chapter_id": v.chapter_id, "chapter_title": v.chapter.title if v.chapter else "完整合同", "version_no": v.version_no, "content": v.content, "created_at": v.created_at.isoformat()} for v in sorted(c.versions, key=lambda x: x.created_at, reverse=True)]

def replace_docx_cell_text(table_cell, value: str):
    paragraphs = table_cell.findall(qn("w:p"))
    if not paragraphs:
        paragraph = OxmlElement("w:p")
        table_cell.append(paragraph)
        paragraphs = [paragraph]
    first = paragraphs[0]
    paragraph_properties = first.find(qn("w:pPr"))
    paragraph_properties = deepcopy(paragraph_properties) if paragraph_properties is not None else None
    first_run = first.find(qn("w:r"))
    run_properties = first_run.find(qn("w:rPr")) if first_run is not None else None
    run_properties = deepcopy(run_properties) if run_properties is not None else None
    for paragraph in paragraphs[1:]:
        table_cell.remove(paragraph)
    for child in list(first):
        if child.tag != qn("w:pPr"):
            first.remove(child)
    for line_index, line in enumerate(value.split("\n") or [""]):
        paragraph = first if line_index == 0 else OxmlElement("w:p")
        if line_index:
            if paragraph_properties is not None:
                paragraph.append(deepcopy(paragraph_properties))
            table_cell.append(paragraph)
        run = OxmlElement("w:r")
        if run_properties is not None:
            run.append(deepcopy(run_properties))
        chunks = line.split("\t")
        for index, chunk in enumerate(chunks):
            if chunk:
                text_node = OxmlElement("w:t")
                if chunk[:1].isspace() or chunk[-1:].isspace():
                    text_node.set(qn("xml:space"), "preserve")
                text_node.text = chunk
                run.append(text_node)
            if index < len(chunks) - 1:
                run.append(OxmlElement("w:tab"))
        paragraph.append(run)

def export_draft_table(doc, block):
    rows = block.get("rows", [])
    columns = max((len(row) for row in rows), default=0)
    if not columns:
        return
    table = doc.add_table(rows=len(rows), cols=columns)
    borders = OxmlElement("w:tblBorders")
    for edge in ("top", "left", "bottom", "right", "insideH", "insideV"):
        border = OxmlElement(f"w:{edge}")
        border.set(qn("w:val"), "single")
        border.set(qn("w:sz"), "4")
        border.set(qn("w:color"), "000000")
        borders.append(border)
    table._tbl.tblPr.insert_element_before(borders, "w:shd", "w:tblLayout", "w:tblCellMar", "w:tblLook")
    column_alignments = block.get("column_alignments", [])
    alignments = {"left": WD_ALIGN_PARAGRAPH.LEFT, "center": WD_ALIGN_PARAGRAPH.CENTER, "right": WD_ALIGN_PARAGRAPH.RIGHT}
    for index, row in enumerate(table.rows):
        is_header = index == 0
        if is_header:
            row._tr.get_or_add_trPr().append(OxmlElement("w:tblHeader"))
        for column, (cell, cell_data) in enumerate(zip(row.cells, rows[index])):
            paragraph = cell.paragraphs[0]
            if column < len(column_alignments):
                paragraph.alignment = alignments.get(column_alignments[column])
            paragraph.add_run(cell_data.get("text", "")).bold = bool(is_header)

def set_run_format(run, *, size=10, bold=None):
    run.font.name = "Microsoft YaHei"
    run.font.size = Pt(size)
    run.font.color.rgb = RGBColor(0x22, 0x22, 0x22)
    if bold is not None:
        run.bold = bold
    properties = run._element.get_or_add_rPr()
    fonts = properties.rFonts
    if fonts is None:
        fonts = OxmlElement("w:rFonts")
        properties.insert(0, fonts)
    for attribute in ("ascii", "hAnsi", "eastAsia", "cs"):
        fonts.set(qn(f"w:{attribute}"), "Microsoft YaHei")

def apply_final_document_format(doc):
    for name, size, bold in (("Normal", 10, False), ("Title", 18, True),
                             ("Heading 1", 14, True), ("Heading 2", 10, False), ("Heading 3", 10, False)):
        style = doc.styles[name]
        style.font.name = "Microsoft YaHei"
        style.font.size = Pt(size)
        style.font.bold = bold
        style.font.color.rgb = RGBColor(0x22, 0x22, 0x22)
        properties = style._element.get_or_add_rPr()
        fonts = properties.rFonts
        if fonts is None:
            fonts = OxmlElement("w:rFonts")
            properties.insert(0, fonts)
        for attribute in ("ascii", "hAnsi", "eastAsia", "cs"):
            fonts.set(qn(f"w:{attribute}"), "Microsoft YaHei")
    for paragraph in doc.paragraphs:
        size = {"Title": 18, "Heading 1": 14, "Heading 2": 10, "Heading 3": 10}.get(paragraph.style.name, 10)
        bold = paragraph.style.name in ("Title", "Heading 1")
        for run in paragraph.runs:
            set_run_format(run, size=size, bold=bold if bold else False)
    for table in doc.tables:
        for row_index, row in enumerate(table.rows):
            for cell in row.cells:
                for paragraph in cell.paragraphs:
                    for run in paragraph.runs:
                        set_run_format(run, size=10, bold=True if row_index == 0 else None)
                for nested in cell.tables:
                    for nested_row in nested.rows:
                        for nested_cell in nested_row.cells:
                            for paragraph in nested_cell.paragraphs:
                                for run in paragraph.runs:
                                    set_run_format(run, size=10)
    for part in (doc.sections[0].header, doc.sections[0].footer):
        for paragraph in part.paragraphs:
            for run in paragraph.runs:
                set_run_format(run, size=10)
    for header in doc.sections:
        for child in list(header.header._element):
            header.header._element.remove(child)
        header.header.add_paragraph()


@app.get("/api/contracts/{contract_id}/export")
def export_contract(contract_id: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
    c = owned_contract(db, contract_id, user.id)
    if not c.final_text: raise HTTPException(409, "合同尚未生成完成")
    try: final_document = json.loads(c.final_document_json or "")
    except (json.JSONDecodeError, TypeError): final_document = None
    _, source_payload = tender_docx_source(c)
    template_mode = bool(source_payload)
    if template_mode:
        try:
            doc = Document(BytesIO(source_payload))
        except Exception as exc:
            raise HTTPException(422, "无法读取已保存的 DOCX，请补传有效的原始标书。") from exc
        source_tables = body_tables(doc)
        source_page = docx_page_template(doc)
        body = doc._element.body
        for child in list(body):
            if child.tag != qn("w:sectPr"):
                body.remove(child)
    else:
        doc = Document()
        source_tables = []
        source_page = {}
    section = doc.sections[0]
    if not template_mode:
        section.top_margin = Inches(0.85); section.bottom_margin = Inches(0.85)
        section.left_margin = Inches(1.0); section.right_margin = Inches(1.0)
    footer_text = (final_document or {}).get("footer", c.document_footer)
    page_format = (final_document or {}).get("page_number_format", c.page_number_format or "第 {page} 页")
    cover_title = (final_document or {}).get("cover_title", c.title)
    contract_number = (final_document or {}).get("contract_number", c.contract_number)
    template_footer = (final_document or {}).get("template_footer", "")
    template_page_format = (final_document or {}).get("template_page_number_format", "")
    footer = section.footer.paragraphs[0]
    update_footer = not template_mode or (
        template_footer == source_page.get("footer")
        and template_page_format == source_page.get("page_number_format")
        and (footer_text != template_footer or page_format != template_page_format)
    )
    if update_footer:
        for child in list(footer._p):
            if child.tag != qn("w:pPr"):
                footer._p.remove(child)
        footer.alignment = WD_ALIGN_PARAGRAPH.CENTER
        if footer_text:
            footer.add_run(footer_text + ("  " if "{page}" in page_format else ""))
    def append_page_number(paragraph, pattern):
        chunks = pattern.split("{page}")
        for index, chunk in enumerate(chunks):
            if chunk: paragraph.add_run(chunk)
            if index < len(chunks) - 1:
                run = OxmlElement("w:r")
                begin = OxmlElement("w:fldChar"); begin.set(qn("w:fldCharType"), "begin"); run.append(begin); paragraph._p.append(run)
                instr_run = OxmlElement("w:r"); instr = OxmlElement("w:instrText"); instr.set(qn("xml:space"), "preserve"); instr.text = " PAGE "; instr_run.append(instr); paragraph._p.append(instr_run)
                separate_run = OxmlElement("w:r"); separate = OxmlElement("w:fldChar"); separate.set(qn("w:fldCharType"), "separate"); separate_run.append(separate); paragraph._p.append(separate_run)
                value_run = OxmlElement("w:r"); value = OxmlElement("w:t"); value.text = "1"; value_run.append(value); paragraph._p.append(value_run)
                end_run = OxmlElement("w:r"); end = OxmlElement("w:fldChar"); end.set(qn("w:fldCharType"), "end"); end_run.append(end); paragraph._p.append(end_run)
    if "{page}" in page_format and update_footer:
        append_page_number(footer, page_format)
    if final_document and final_document.get("sections"):
        content_sections = [item for item in final_document["sections"] if not item.get("source_material")]
        for section_index, item in enumerate(content_sections):
            if section_index:
                doc.add_page_break()
            is_cover = bool(item.get("is_cover"))
            title = str(item.get("title") or "未命名阶段")
            if is_cover:
                cover_heading = doc.add_heading(cover_title or c.title, level=0)
                cover_heading.alignment = WD_ALIGN_PARAGRAPH.CENTER
                if contract_number:
                    number_paragraph = doc.add_paragraph(contract_number)
                    number_paragraph.alignment = WD_ALIGN_PARAGRAPH.CENTER
            else:
                doc.add_heading(title, level=1)
            for block in item.get("blocks", []):
                kind = block.get("kind")
                if kind == "table":
                    export_draft_table(doc, block)
                    continue
                text_value = str(block.get("text") or "")
                if not text_value.strip(): continue
                if kind == "heading":
                    paragraph = doc.add_heading(text_value, level=2)
                elif kind == "subheading":
                    paragraph = doc.add_heading(text_value, level=3)
                else:
                    paragraph = doc.add_paragraph(text_value)
                if is_cover:
                    paragraph.alignment = WD_ALIGN_PARAGRAPH.CENTER
        if content_sections and not content_sections[0].get("is_cover"):
            doc.core_properties.title = c.title
    else:
        doc.add_heading(c.title, 0)
        for block in c.final_text.split("\n\n"):
            if block.strip(): doc.add_paragraph(block)
    if source_tables:
        doc.add_page_break()
        doc.add_heading("标书原始表格", level=1)
        draft = final_document or {}
        saved_revision = draft.get("tender_layout_revision")
        saved_tables = {}
        if not saved_revision or saved_revision == sha256(source_payload).hexdigest():
            for item in draft.get("sections", []):
                if item.get("source_material"):
                    saved_tables.update({block.get("source_table_index"): block for block in item.get("blocks", []) if block.get("kind") == "table"})
        for table_index, source_table in enumerate(source_tables):
            table_xml = deepcopy(source_table)
            saved = saved_tables.get(table_index, {})
            cell_updates = {cell["source_cell_index"]: cell for row in saved.get("rows", []) for cell in row
                            if cell.get("source_cell_index") is not None and cell.get("display", True)}
            table_cells = (cell for row in table_xml.tr_lst for cell in row.tc_lst)
            for index, table_cell in enumerate(table_cells):
                value = cell_updates.get(index)
                if value and value.get("text") != value.get("original_text") and value.get("original_text") == extract_docx_cell_text(table_cell):
                    replace_docx_cell_text(table_cell, value["text"])
            body.insert_element_before(table_xml, "w:sectPr")
    apply_final_document_format(doc)
    buff = BytesIO(); doc.save(buff); buff.seek(0)
    return StreamingResponse(buff, media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document", headers={"Content-Disposition": f'attachment; filename="contract-{c.id}.docx"'})
