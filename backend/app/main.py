from pathlib import Path
from datetime import datetime, timezone
import json
from uuid import uuid4
from fastapi import FastAPI, Depends, HTTPException, BackgroundTasks
from fastapi import File, Form, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session
from sqlalchemy import select, func, inspect, text
from io import BytesIO
from docx import Document
from .config import settings
from .database import Base, engine, get_db, SessionLocal
from .models import User, Contract, Chapter, Version, WorkflowEvent
from .schemas import Credentials, TokenOut, PromptUpdate, ModelUpdate, StageModelUpdate, ChapterCreate, ChapterTitleUpdate, ChapterOrderUpdate, ModelName
from .auth import hash_password, verify_password, make_token, current_user
from .generator import workflow, cover_plan, long_prompt, draft_stage, merge_node, normalize_model_text

Path("data").mkdir(exist_ok=True)
Base.metadata.create_all(bind=engine)
with engine.begin() as connection:
    contract_columns = {column["name"] for column in inspect(connection).get_columns("contracts")}
    contract_migrations = {
        "node_models": "TEXT NOT NULL DEFAULT '{}'", "contract_number": "VARCHAR(120) NOT NULL DEFAULT ''",
        "generation_id": "VARCHAR(36) NOT NULL DEFAULT ''",
        "deleted_at": "DATETIME NULL",
        "tender_filename": "VARCHAR(255) NOT NULL DEFAULT ''", "tender_text": "TEXT NOT NULL DEFAULT ''",
        "reference_filename": "VARCHAR(255) NOT NULL DEFAULT ''", "reference_text": "TEXT NOT NULL DEFAULT ''",
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

def contract_dict(c: Contract):
    try: node_models = json.loads(c.node_models or "{}")
    except (json.JSONDecodeError, TypeError): node_models = {}
    return {"id": c.id, "title": c.title, "contract_number": c.contract_number,
            "requirements": c.requirements, "tender_filename": c.tender_filename,
            "reference_filename": c.reference_filename, "status": c.status,
            "final_text": c.final_text, "final_check": c.final_check,
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

def is_current_generation(db: Session, contract_id: int, generation_id: str | None) -> bool:
    if not generation_id:
        return True
    current_id = db.scalar(select(Contract.generation_id).where(Contract.id == contract_id))
    return current_id == generation_id

def gateway_progress_callback(contract_id: int, generation_id: str | None = None):
    def callback(state: str, attempt: int, delay: int, status_code: int):
        progress_db = SessionLocal()
        try:
            if not is_current_generation(progress_db, contract_id, generation_id):
                return
            if state == "retry":
                reason = f"HTTP {status_code}" if status_code else "接口无响应"
                message = f"模型网关单一接口第 {attempt} 次请求遇到 {reason}；{delay} 秒后在同一地址重试（最多 3 次）。"
                event_status = "running"
            elif state == "recovered":
                message = f"模型网关重试后恢复，已在第 {attempt} 次请求成功。"
                event_status = "completed"
            else:
                reason = f"HTTP {status_code}" if status_code else "接口无响应"
                message = f"模型网关单一接口连续 {attempt} 次请求失败（{reason}），自动重试次数已用尽。"
                event_status = "failed"
            record_event(progress_db, contract_id, "gateway_retry", "LangGraph · 模型网关重试",
                         event_status, message)
        finally:
            progress_db.close()
    return callback

async def extract_document(upload: UploadFile | None) -> tuple[str, str]:
    if upload is None or not upload.filename:
        return "", ""
    filename = Path(upload.filename).name
    payload = await upload.read()
    extension = Path(filename).suffix.lower()
    try:
        if extension in (".txt", ".md", ".text"):
            try: content = payload.decode("utf-8-sig")
            except UnicodeDecodeError: content = payload.decode("gb18030")
        elif extension == ".docx":
            doc = Document(BytesIO(payload))
            blocks = [paragraph.text.strip() for paragraph in doc.paragraphs if paragraph.text.strip()]
            for table in doc.tables:
                for row in table.rows:
                    row_text = " | ".join(cell.text.strip().replace("\n", " ") for cell in row.cells if cell.text.strip())
                    if row_text: blocks.append(row_text)
            content = "\n".join(blocks)
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
    return filename, content[:120000]

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
        c.status = "planning"
        if not is_current_generation(db, contract_id, generation_id):
            db.rollback()
            return
        db.commit()
        gateway_progress = gateway_progress_callback(contract_id, generation_id)
        try: node_models = json.loads(c.node_models or "{}")
        except (json.JSONDecodeError, TypeError): node_models = {}
        node_models = {"planner": "qwen3.8-flash", "chapter_default": "qwen3.8-flash", **node_models}
        record_event(db, c.id, "tender_parse", "LangGraph · 标书解析", "completed",
                     "已读取合同记录中保存的标书解析文本。" if c.tender_text else "未上传标书，跳过解析。")
        record_event(db, c.id, "reference_parse", "LangGraph · 参考合同解析", "completed",
                     "已读取合同记录中保存的参考合同文本，用于匹配章节结构与行文风格。" if c.reference_text else "未上传参考合同，跳过解析。")
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
                         "读取合同要求与已上传文件，规划带有对应标书和参考文本的阶段节点。", node_models["planner"])
            plans = None
            plan_count = 0
        state = {"requirements": c.requirements, "node_models": node_models,
                 "contract_title": c.title, "contract_number": c.contract_number,
                 "tender_text": c.tender_text, "reference_text": c.reference_text,
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
                          tender_file: UploadFile | None = File(default=None), reference_file: UploadFile | None = File(default=None),
                          db: Session = Depends(get_db),
                          user: User = Depends(current_user)):
    if not title.strip() or not contract_number.strip(): raise HTTPException(422, "合同名称和合同编号不能为空")
    if len(title.strip()) > 200 or len(contract_number.strip()) > 120 or len(requirements) > 20000:
        raise HTTPException(422, "合同名称、编号或补充要求超过长度限制")
    tender_filename, tender_text = await extract_document(tender_file)
    reference_filename, reference_text = await extract_document(reference_file)
    if len(requirements.strip()) < 10 and not tender_text:
        raise HTTPException(422, "请填写至少 10 个字的合同要求，或上传标书作为拆分依据")
    node_models = {"planner": planner_model, "chapter_default": chapter_model}
    c = Contract(user_id=user.id, title=title.strip(), contract_number=contract_number.strip(),
                 requirements=requirements.strip(), tender_filename=tender_filename, tender_text=tender_text,
                 reference_filename=reference_filename, reference_text=reference_text, status="queued",
                 generation_id=str(uuid4()), node_models=json.dumps(node_models)); db.add(c); db.commit(); db.refresh(c)
    tasks.add_task(run_generation, c.id, c.generation_id)
    return {"id": c.id, "status": c.status}

@app.get("/api/contracts/{contract_id}")
def get_contract(contract_id: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
    return contract_dict(owned_contract(db, contract_id, user.id))

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
    c.final_text = ""; c.final_check = ""; c.status = "pending"
    db.commit()
    record_event(db, c.id, f"chapter_trash:{chapter.id}", f"回收站 · {chapter.title}", "completed", "阶段已移入回收站，可恢复。")
    return {"status": "trashed"}

@app.get("/api/recycle-bin")
def get_recycle_bin(db: Session = Depends(get_db), user: User = Depends(current_user)):
    deleted_contracts = db.scalars(select(Contract).where(
        Contract.user_id == user.id, Contract.deleted_at.is_not(None)
    ).order_by(Contract.deleted_at.desc())).all()
    deleted_chapters = db.scalars(select(Chapter, Contract).join(Contract).where(
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
    c.final_text = ""; c.final_check = ""; c.status = "pending"
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
    c.final_text = ""; c.final_check = ""; c.status = "pending"
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
    c.final_text = ""; c.final_check = ""; c.status = "pending"
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
    c.status = "queued"; c.final_text = ""; c.final_check = ""; c.generation_id = str(uuid4())
    for chapter in c.chapters:
        if chapter.deleted_at is not None: continue
        chapter.status = "pending"; chapter.content = ""; chapter.self_check = ""
    db.commit(); tasks.add_task(run_generation, c.id, c.generation_id); return {"status": "queued"}

@app.post("/api/contracts/{contract_id}/restart", status_code=202)
def restart_all(contract_id: int, tasks: BackgroundTasks, db: Session = Depends(get_db), user: User = Depends(current_user)):
    c = owned_contract(db, contract_id, user.id)
    c.generation_id = str(uuid4())
    c.status = "queued"; c.final_text = ""; c.final_check = ""
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

@app.get("/api/contracts/{contract_id}/export")
def export_contract(contract_id: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
    c = owned_contract(db, contract_id, user.id)
    if not c.final_text: raise HTTPException(409, "合同尚未生成完成")
    doc = Document(); doc.add_heading(c.title, 0)
    for block in c.final_text.split("\n\n"):
        if block.strip(): doc.add_paragraph(block)
    buff = BytesIO(); doc.save(buff); buff.seek(0)
    return StreamingResponse(buff, media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document", headers={"Content-Disposition": f'attachment; filename="contract-{c.id}.docx"'})
