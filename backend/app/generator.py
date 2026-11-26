"""LangGraph contract workflow. Every inference call goes through the configured gateway."""
import json
import ast
import logging
import time
from typing import Annotated, Callable, TypedDict
import operator
import re
import httpx
from langgraph.graph import StateGraph, START, END
from langgraph.types import Send
from .config import settings

MODEL_OPTIONS = ("qwen3.8-flash", "qwen3.8-max")
DEFAULT_MODEL = "qwen3.8-flash"

def normalize_model_text(value) -> str:
    if value is None:
        return ""
    if isinstance(value, list):
        return "\n".join(f"• {normalize_model_text(item)}" for item in value if normalize_model_text(item))
    if isinstance(value, dict):
        return "\n".join(f"{key}：{normalize_model_text(item)}" for key, item in value.items() if normalize_model_text(item))
    text = str(value).strip()
    if len(text) >= 2 and text[0] in "[{" and text[-1] in "]}":
        for parser in (json.loads, ast.literal_eval):
            try:
                parsed = parser(text)
            except (ValueError, SyntaxError, json.JSONDecodeError):
                continue
            if isinstance(parsed, (list, dict)):
                return normalize_model_text(parsed)
            if isinstance(parsed, str) and parsed != text:
                return normalize_model_text(parsed)
    return text

PROMPT_GUIDANCE = """起草与检查时请始终遵守以下工作规则：先识别用户提供的事实、明确要求、未决信息和潜在风险，不得把推测写成已经确认的事实。合同语言应准确、克制、可执行，避免含糊词语、口号式表达、无依据的法律结论和与交易无关的模板内容。条款应说明适用主体、触发条件、履行期限、所需材料、验收或确认方式、责任承担及例外情形；没有足够信息时使用【待补充】并指出应由哪一方补充。使用统一的主体名称、定义、日期、金额、计量单位和条款称谓，注意前后引用正确，避免重复、冲突或遗漏。先依据检查标准逐项核验，再完成输出；发现冲突时优先保留用户明确指示，并将无法自行消解的事项列为待确认。输出结构清晰、适合直接纳入正式合同，采用中文标题和编号，段落简洁但不省略必要条件。不得输出隐藏思维链、内部推演或无关说明，只提交当前阶段要求的计划、条款、检查结论或完整文本。"""

def long_prompt(text: str) -> str:
    """Keep every built-in stage prompt at least 500 Chinese characters."""
    value = text.strip()
    while len(value) < 500:
        value += "\n\n" + PROMPT_GUIDANCE
    return value

PLANNER_PROMPT = long_prompt("""你是合同项目的 LangGraph 需求规划节点。优先把用户上传的标书正文作为需求拆分的事实依据，结合用户补充要求识别合同类型、交易目标、主体、标的、履约、交付、验收、价款、期限、权利义务、保密、知识产权、违约、解除和争议解决。上传的参考文档只用于匹配相应章节的行文风格、层级和结构，不得把参考文档中的主体、金额或事实直接冒充为本项目事实。第一个阶段必须是“封面”，包含合同名称和编号。其后按照合同逻辑拆分阶段；每个阶段给出标题、简洁描述、知识点、可执行的生成计划、逐项自查标准、至少500字的详细默认提示词、与当前阶段对应的标书原文摘录 source_text，以及参考文档对应章节的原文摘录 reference_text。请严格从给定上传文本中摘录，不要杜撰引用；没有对应段落时返回空字符串。提示词要明确如何结合标书事实、参考文档的结构与行文风、待补充信息和检查重点。最终只输出合法 JSON 数组，每项字段为 title、description、knowledge_points、plan、check_criteria、prompt、source_text、reference_text；不要输出 Markdown 围栏或数组外文字。""")

PLANNER_NO_REFERENCE_PROMPT = long_prompt("""你是合同项目的 LangGraph 需求规划节点。本项目没有上传参考文档，这是正常输入；不要等待参考文档，不要尝试匹配或推测参考合同结构和文风。只依据用户补充要求和标书压缩文本，先整体理解项目背景、采购目标、标的范围、技术与商务要求、主体职责、实施计划、交付成果、验收标准、价款支付、期限、服务保障、违约责任及其他明确约束，再按标书实际要求拆分合同阶段。不得把标书中未出现的内容写成既定事实；必要但缺失的信息标记为【待补充】。第一个阶段必须是“封面”，包含合同名称和编号。每个阶段应覆盖标书中对应的要求，避免遗漏、重复和凭空增加章节；给出标题、简洁描述、知识点、可执行的生成计划、逐项自查标准、至少500字的详细默认提示词，以及可从标书原文核实的 source_text。由于没有参考文档，每个阶段的 reference_text 必须是空字符串，且各阶段提示词不得要求依赖参考文档。标书没有上传时，改以用户补充要求为拆分依据。最终只输出合法 JSON 数组，每项字段为 title、description、knowledge_points、plan、check_criteria、prompt、source_text、reference_text；不要输出 Markdown 围栏或数组外文字。""")

OPENING_OVERVIEW_TITLE = "开标一览表"
OPENING_OVERVIEW_PROMPT = long_prompt("""本阶段是标书原表展示节点。只把标书中“开标一览表”的原表格按原有行列直接展示，不改写、不总结、不补充、不推测单元格内容；表格结构和单元格文字必须原样保留。若源表内容不可用，明确写【需补传原标书】。""")

def opening_overview_excerpt(tender_text: str) -> str:
    lines = (tender_text or "").splitlines()
    starts = [index for index, line in enumerate(lines)
              if re.fullmatch(r"\[\[TABLE_\d+_START\]\]", line.strip())]
    for start in starts:
        table_id = re.search(r"TABLE_(\d+)_START", lines[start]).group(1)
        end = next((index for index in range(start + 1, len(lines))
                    if re.fullmatch(rf"\[\[TABLE_{table_id}_END\]\]", lines[index].strip())), None)
        if end is None:
            continue
        context = "\n".join(lines[max(0, start - 4):end + 1])
        if OPENING_OVERVIEW_TITLE in context:
            return f"【{OPENING_OVERVIEW_TITLE}（必须按标书原表展示）】\n" + "\n".join(lines[start:end + 1])
    title_positions = [index for index, line in enumerate(lines) if OPENING_OVERVIEW_TITLE in line]
    if title_positions:
        groups = []
        current = []
        for line in lines:
            if "|" in line:
                current.append(line)
            elif current:
                groups.append(current)
                current = []
        if current:
            groups.append(current)
        if groups:
            title = min(title_positions, key=lambda index: min(abs(index - lines.index(row)) for group in groups for row in group))
            group = min(groups, key=lambda rows: min(abs(title - lines.index(row)) for row in rows))
            table_id = 0
            table_rows = []
            for row_index, line in enumerate(group, start=1):
                cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
                table_rows.append(f"表格 {table_id} 第 {row_index} 行：" + " | ".join(cells))
            return (f"【{OPENING_OVERVIEW_TITLE}（必须按标书原表展示）】\n[[TABLE_{table_id}_START]]\n"
                    + "\n".join(table_rows) + f"\n[[TABLE_{table_id}_END]]")
    return ""

def opening_overview_plan(tender_text: str, chapter_model: str = DEFAULT_MODEL) -> dict | None:
    excerpt = opening_overview_excerpt(tender_text)
    if not excerpt:
        return None
    return {"title": OPENING_OVERVIEW_TITLE,
            "description": "单独展示标书中的开标一览表原表。",
            "knowledge_points": "开标一览表原始行列与单元格内容",
            "plan": "直接展示标书中的开标一览表，不改写或省略表格内容。",
            "check_criteria": "必须包含标书开标一览表的原始表格及全部单元格内容，不得改写、总结或省略。",
            "prompt": OPENING_OVERVIEW_PROMPT,
            "source_text": excerpt, "reference_text": "", "model": chapter_model}

CHAPTER_SYSTEM_PROMPT = long_prompt("""你是合同章节生成节点，必须使用所选百炼模型根据用户提示词和 LangGraph 规划结果起草当前章节。先核对用户要求、章节计划、知识点和检查标准之间的关系；只写当前节点负责的内容，不能替其他章节擅自作出未授权安排。条款应具体写明主体、行为、时点、条件、协作、交付或验收方式及违约后果中适用的要素。对缺失事实采用【待补充】标识，并用自然、严谨、可签署的中文组织标题和编号。明确区分义务、权利、条件、例外和程序，尽量避免“及时”“尽快”“相关”等无法执行的表达；如确需使用，应说明期限或确认办法。保持金额、日期、术语和主体称谓一致，不得凭空引用法规条文、外部模板或用户未提供的商业承诺。不得输出分析过程，只输出可供合同合并节点使用的章节正文。""")

REVIEW_SYSTEM_PROMPT = long_prompt("""你是合同章节自查节点。请按照给定检查标准逐项核对本节点文本，并结合原始需求、章节知识点和计划，检查遗漏、歧义、前后不一致、无依据事实、主体错配、期限金额缺失、责任失衡、无法操作的条件、错误引用和不合适的重复内容。对于每个发现，说明对应文本或缺项、可能影响以及具体修改建议；没有发现时也要明确说明检查覆盖范围。不能把自查描述成法律意见或签署保证，不能重写其他章节。优先指出会妨碍履行或造成重大争议的事项，再列普通完善建议；所有未知信息仍应保留【待补充】提示。最终输出简明但充分的自查结论和修改建议，不输出隐藏推理链或与本合同无关的说明。""")

MERGE_SYSTEM_PROMPT = long_prompt("""你是 LangGraph 的完整合同合并节点。输入章节可能全部完成，也可能仍有阶段正在生成。请根据规划顺序将每章正文合并为一份连续、可阅读的合同文本，保留经节点生成的实质条款、章节标题、层级和编号；统一当事方名称、术语、日期、金额格式和条款引用，修正明显的格式断裂与重复标题。若某一阶段内容是【该阶段尚未生成，生成完成后可再次合并】，必须在对应标题下原样保留此占位标记，不得代写或推测该阶段的合同条款。不得因为追求简短而删除已生成内容中的权利义务、条件、程序或责任，不得新增用户未提出的商业条件或虚构事实。若发现章节间存在无法安全消解的冲突，应保留相关约定并用【需确认：……】标明，不要擅自替用户作选择。将待补充项保留在适当位置。只输出合并后的完整合同正文，不输出过程说明、摘要、检查报告或隐藏推理内容。""")

def ask(system: str, user: str, model: str = DEFAULT_MODEL,
        retry_callback: Callable[..., None] | None = None,
        node_key: str = "", phase: str = "") -> str:
    if not settings.dashscope_api_key:
        raise RuntimeError("未配置 DASHSCOPE_API_KEY，无法调用模型网关")
    if model not in MODEL_OPTIONS:
        raise RuntimeError(f"不支持的推理模型：{model}")
    configured_endpoint = settings.dashscope_base_url.rstrip("/")
    if configured_endpoint.endswith("/chat/completions"):
        endpoint_root = configured_endpoint.removesuffix("/chat/completions")
    elif configured_endpoint.endswith("/responses"):
        endpoint_root = configured_endpoint.removesuffix("/responses")
    else:
        endpoint_root = configured_endpoint.rsplit("/", 1)[0]
    endpoints = [f"{endpoint_root}/chat/completions", f"{endpoint_root}/responses"]
    system_text = long_prompt(system)
    retry_delays = (2, 5)
    attempt = 0
    while True:
        attempt += 1
        endpoint = endpoints[(attempt - 1) % len(endpoints)]
        if endpoint.endswith("/responses"):
            payload = {"model": model, "input": [{"role": "system", "content": system_text},
                                                   {"role": "user", "content": user}], "temperature": 0.1,
                       "stream": True}
        else:
            payload = {"model": model, "messages": [{"role": "system", "content": system_text},
                                                       {"role": "user", "content": user}], "temperature": 0.1,
                       "stream": True, "stream_options": {"include_usage": True}}
        try:
            timeout = httpx.Timeout(settings.dashscope_timeout_seconds, connect=2000, write=12000, pool=200)
            with httpx.Client(proxy=None, trust_env=False, timeout=timeout) as client:
                with client.stream("POST", endpoint,
                                   headers={"Authorization": f"Bearer {settings.dashscope_api_key}",
                                            "Content-Type": "application/json", "Accept": "text/event-stream"},
                                   json=payload) as response:
                    if response.status_code in (400, 404, 405, 408, 415, 422, 429, 500, 502, 503, 504):
                        body = response.read().decode("utf-8", "replace")[:600]
                        status_code = response.status_code
                    else:
                        response.raise_for_status()
                        pieces = []
                        first_output_reported = False
                        for line in response.iter_lines():
                            if not line or not line.startswith("data:"):
                                continue
                            raw_event = line[5:].strip()
                            if raw_event == "[DONE]":
                                break
                            try:
                                event = json.loads(raw_event)
                            except (ValueError, TypeError):
                                continue
                            if endpoint.endswith("/responses"):
                                event_type = event.get("type", "")
                                if event_type in ("error", "response.failed", "response.incomplete"):
                                    error = event.get("error") or event.get("response", {}).get("error") or event
                                    raise httpx.TransportError(f"模型网关流式响应失败：{str(error)[:400]}")
                                delta = event.get("delta", "")
                                is_output = event_type.endswith(".delta") and bool(delta)
                                if is_output and not first_output_reported and retry_callback:
                                    try: retry_callback("streaming", attempt, 0, 200, node_key, phase, model)
                                    except Exception: logging.exception("Gateway streaming progress callback failed")
                                    first_output_reported = True
                                if event_type == "response.output_text.delta":
                                    pieces.append(str(delta))
                                elif event_type == "response.completed":
                                    response_object = event.get("response") or {}
                                    if not pieces:
                                        text = response_object.get("output_text", "")
                                        if not text:
                                            text = "".join(str(part.get("text", ""))
                                                           for item in response_object.get("output", [])
                                                           if item.get("type") == "message"
                                                           for part in item.get("content", [])
                                                           if part.get("type") in ("output_text", "text"))
                                        if text:
                                            pieces.append(text)
                            else:
                                if event.get("error"):
                                    raise httpx.TransportError(f"模型网关流式响应失败：{str(event['error'])[:400]}")
                                for choice in event.get("choices", []):
                                    delta = choice.get("delta") or {}
                                    content = delta.get("content")
                                    reasoning = delta.get("reasoning_content")
                                    if (content or reasoning) and not first_output_reported and retry_callback:
                                        try: retry_callback("streaming", attempt, 0, 200, node_key, phase, model)
                                        except Exception: logging.exception("Gateway streaming progress callback failed")
                                        first_output_reported = True
                                    if isinstance(content, str):
                                        pieces.append(content)
                        body = ""
                        status_code = response.status_code
                        content = "".join(pieces)
            if status_code in (400, 404, 405, 408, 415, 422, 429, 500, 502, 503, 504):
                if attempt <= len(retry_delays):
                    retry_after = response.headers.get("Retry-After", "")
                    delay = min(max(int(retry_after), 1), 30) if retry_after.isdigit() else retry_delays[attempt - 1]
                    if retry_callback:
                        try: retry_callback("retry", attempt, delay, status_code, node_key, phase, model)
                        except Exception: logging.exception("Gateway retry progress callback failed")
                    time.sleep(delay)
                    continue
                if retry_callback:
                    try: retry_callback("failed", attempt, 0, status_code, node_key, phase, model)
                    except Exception: logging.exception("Gateway retry progress callback failed")
                raise RuntimeError(f"模型网关返回 HTTP {status_code}：{body}")
            if isinstance(content, list):
                content = "".join(str(part.get("text", "")) for part in content if isinstance(part, dict))
            if not str(content).strip():
                if attempt <= len(retry_delays):
                    delay = retry_delays[attempt - 1]
                    if retry_callback:
                        try: retry_callback("retry", attempt, delay, 0, node_key, phase, model)
                        except Exception: logging.exception("Gateway retry progress callback failed")
                    time.sleep(delay)
                    continue
                if retry_callback:
                    try: retry_callback("failed", attempt, 0, 0, node_key, phase, model)
                    except Exception: logging.exception("Gateway retry progress callback failed")
                raise RuntimeError("模型网关返回了空内容")
            if attempt > 1 and retry_callback:
                try: retry_callback("recovered", attempt, 0, 200, node_key, phase, model)
                except Exception: logging.exception("Gateway retry progress callback failed")
            return str(content).strip()
        except httpx.HTTPStatusError as exc:
            detail = exc.response.text[:600]
            raise RuntimeError(f"模型网关返回 HTTP {exc.response.status_code}：{detail}") from exc
        except httpx.TransportError as exc:
            if attempt <= len(retry_delays):
                delay = retry_delays[attempt - 1]
                if retry_callback:
                    try: retry_callback("retry", attempt, delay, 0, node_key, phase, model)
                    except Exception: logging.exception("Gateway retry progress callback failed")
                time.sleep(delay)
                continue
            if retry_callback:
                try: retry_callback("failed", attempt, 0, 0, node_key, phase, model)
                except Exception: logging.exception("Gateway retry progress callback failed")
            raise RuntimeError(f"模型网关两个接口轮流请求后仍无响应（共 {attempt} 次）：{str(exc)[:400]}") from exc
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            if attempt <= len(retry_delays):
                delay = retry_delays[attempt - 1]
                if retry_callback:
                    try: retry_callback("retry", attempt, delay, 0, node_key, phase, model)
                    except Exception: logging.exception("Gateway retry progress callback failed")
                time.sleep(delay)
                continue
            if retry_callback:
                try: retry_callback("failed", attempt, 0, 0, node_key, phase, model)
                except Exception: logging.exception("Gateway retry progress callback failed")
            raise RuntimeError(f"模型网关两个接口轮流请求后仍返回无法识别的响应：{str(exc)[:400]}") from exc

def relevant_excerpt(document: str, title: str, knowledge_points: str, limit: int = 5000) -> str:
    if not document:
        return ""
    paragraphs = []
    source_label = ""
    for part in document.splitlines():
        part = part.strip()
        if not part:
            continue
        if part.startswith("【参考文档：") and part.endswith("】"):
            source_label = part
            continue
        paragraphs.append((source_label, part))
    keywords = [term.strip() for term in (title + "、" + knowledge_points).replace("，", "、").replace(",", "、").split("、") if len(term.strip()) >= 2]
    ranked = sorted(((sum(paragraph.count(key) for key in keywords), index, paragraph)
                     for index, (_, paragraph) in enumerate(paragraphs)), reverse=True)
    relevant = [item for item in ranked if item[0] > 0][:8]
    if not relevant:
        return ""
    chosen = sorted(index for _, index, _ in relevant)
    excerpts = []
    previous_label = None
    for index in chosen:
        label, paragraph = paragraphs[index]
        if label and label != previous_label:
            excerpts.append(label)
        excerpts.append(paragraph)
        previous_label = label
    return "\n".join(excerpts)[:limit]

def compact_document_text(document: str, limit: int = 30000) -> str:
    """Build and persist an extractive, one-pass planning view while retaining raw source text."""
    if not document:
        return ""
    lines = []
    seen = set()
    table_stack = []
    for raw_line in document.replace("\r", "\n").splitlines():
        line = re.sub(r"\s+", " ", raw_line).strip()
        if not line:
            continue
        marker = re.fullmatch(r"\[\[TABLE_(\d+)_(START|END)\]\]", line)
        inside_table = bool(table_stack) or bool(marker and marker[2] == "START")
        if not inside_table and line in seen:
            continue
        if not inside_table:
            seen.add(line)
        lines.append(line)
        if marker and marker[2] == "START":
            table_stack.append(marker[1])
        elif marker and marker[2] == "END" and marker[1] in table_stack:
            table_stack = table_stack[:len(table_stack) - 1 - table_stack[::-1].index(marker[1])]
    normalized = "\n".join(lines)
    if len(normalized) <= limit:
        return normalized

    # Tables are source material, so retain every marked table block even when
    # that makes the planning view longer than its nominal text budget.
    table_indices = set()
    table_stack = []
    for index, line in enumerate(lines):
        marker = re.fullmatch(r"\[\[TABLE_(\d+)_(START|END)\]\]", line)
        if marker and marker[2] == "START":
            table_stack.append(marker[1])
        if table_stack:
            table_indices.add(index)
        if marker and marker[2] == "END" and marker[1] in table_stack:
            table_stack = table_stack[:len(table_stack) - 1 - table_stack[::-1].index(marker[1])]

    important = re.compile(r"招标|投标|标书|采购|项目|合同|需求|范围|服务|技术|商务|交付|验收|质量|标准|期限|工期|进度|价款|报价|付款|支付|违约|责任|保密|知识产权|资质|资格|评分|响应|义务|权利|结算|保证|履约|风险|security|scope|delivery|acceptance|payment|requirement|qualification|technical|commercial", re.I)
    heading = re.compile(r"^(第[一二三四五六七八九十百0-9]+[章节条部分]|[一二三四五六七八九十]+[、.．]|\d+(?:\.\d+){0,3}[、.． ]|【[^】]{1,60}】)")
    scored = []
    for index, line in enumerate(lines):
        score = (5 if important.search(line) else 0) + (4 if heading.search(line) else 0)
        if len(line) < 180:
            score += 1
        if len(line) > 1800:
            score -= 2
        scored.append((score, index, line))
    # Keep the document boundaries and prioritize clause headings and requirement-bearing lines.
    chosen = {0, len(lines) - 1, *table_indices}
    budget = max(0, limit - len(lines))
    used = sum(len(lines[i]) for i in chosen)
    for _, index, line in sorted(scored, key=lambda item: (-item[0], item[1])):
        if index in chosen or used + len(line) > budget:
            continue
        chosen.add(index)
        used += len(line)
    return "\n".join(lines[index] for index in sorted(chosen))

def verified_excerpt(candidate: str, document: str, title: str, knowledge_points: str) -> str:
    candidate = candidate.strip()
    if candidate and document and candidate in document:
        return candidate[:5000]
    return relevant_excerpt(document, title, knowledge_points)

def cover_plan(contract_title: str, contract_number: str, tender_text: str = "", reference_text: str = "", chapter_model: str = DEFAULT_MODEL) -> dict:
    knowledge = "合同名称、合同编号、签约主体及签署日期等封面信息"
    prompt = f"请生成合同封面。合同名称：{contract_title}。合同编号：{contract_number}。如有标书封面或参考文本，参照其适当的排版层级和简洁表达，但必须保留本项目合同名称与编号，不得复制参考项目事实。未提供的主体、签署地点和日期使用【待补充】。\n{PROMPT_GUIDANCE}"
    return {"title": "封面", "description": "展示合同名称和合同编号，其他封面信息以待补充标记。", "knowledge_points": knowledge,
            "plan": "将封面固定为合同第一阶段，准确展示合同名称与合同编号；其他未提供信息以【待补充】标识。",
            "check_criteria": "封面是首个阶段；合同名称和编号与项目字段完全一致；不得虚构主体或签署日期。",
            "prompt": long_prompt(prompt), "source_text": relevant_excerpt(tender_text, "封面", knowledge),
            "reference_text": relevant_excerpt(reference_text, "封面", knowledge), "model": chapter_model}

STAGE_DESIGN_PROMPT = long_prompt("""你是合同阶段设计节点。用户已经提供了阶段名称和阶段描述，请结合项目合同名称、编号、核心要求，以及数据库中保存的标书全文和参考文档全文，为这个新阶段自动补全执行所需的信息。优先从标书中识别与该阶段相关的事实依据和原文摘录；从参考文档中找出对应章节的结构、术语层级和行文样例，并把真实原文摘录保存为 reference_text。参考文档只用于文风和结构，不得将参考项目的主体、金额、日期、责任安排直接挪到本合同。请生成清楚的阶段描述、知识点、详细生成计划、可逐条核对的检查标准，以及至少500字的默认起草提示词。提示词必须要求下游节点结合匹配到的标书依据和参考文本，标明未知事实、不要编造，并遵循本合同名称和编号。若没有找到相关原文，source_text 或 reference_text 返回空字符串，不得杜撰引文。只返回合法 JSON 对象，字段必须为 description、knowledge_points、plan、check_criteria、prompt、source_text、reference_text，不要返回 Markdown 围栏或额外解释。""")

def draft_stage(title: str, description: str, contract_title: str, contract_number: str,
                requirements: str, tender_text: str, reference_text: str, model: str,
                retry_callback: Callable[..., None] | None = None) -> dict:
    tender_excerpt = relevant_excerpt(tender_text, title, description, limit=12000) or compact_document_text(tender_text, 12000)
    reference_excerpt = relevant_excerpt(reference_text, title, description, limit=10000) or compact_document_text(reference_text, 10000)
    user_prompt = (f"合同名称：{contract_title}\n合同编号：{contract_number}\n合同核心要求：\n{requirements or '主要依据上传标书'}\n\n"
                   f"阶段名称：{title}\n用户描述：\n{description}\n\n标书相关文本（由已保存的解析文本提取）：\n{tender_excerpt or '未上传标书'}\n\n"
                   f"参考文档相关文本（由已保存的解析文本提取）：\n{reference_excerpt or '未上传参考文档'}")
    raw = ask(STAGE_DESIGN_PROMPT, user_prompt, model, retry_callback,
              node_key=f"stage_design:{title}", phase="阶段计划生成").strip()
    if raw.startswith("```"):
        raw = raw.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"阶段设计节点未返回有效 JSON：{exc.msg}") from exc
    if not isinstance(parsed, dict):
        raise RuntimeError("阶段设计节点未返回阶段对象")
    knowledge = normalize_model_text(parsed.get("knowledge_points", ""))
    plan = normalize_model_text(parsed.get("plan", ""))
    criteria = normalize_model_text(parsed.get("check_criteria", ""))
    prompt = normalize_model_text(parsed.get("prompt", ""))
    if not plan or not criteria or not prompt:
        raise RuntimeError("阶段设计未生成完整的计划、检查标准或提示词")
    return {"description": normalize_model_text(parsed.get("description", "")) or description,
            "knowledge_points": knowledge, "plan": plan, "check_criteria": criteria,
            "prompt": long_prompt(prompt),
            "source_text": verified_excerpt(str(parsed.get("source_text", "")), tender_text, title, knowledge),
            "reference_text": verified_excerpt(str(parsed.get("reference_text", "")), reference_text, title, knowledge),
            "model": model}

def parse_plans(raw: str, requirements: str, chapter_model: str = DEFAULT_MODEL,
                contract_title: str = "合同", contract_number: str = "【待补充】",
                tender_text: str = "", reference_text: str = "") -> list[dict]:
    text = raw.strip()
    if text.startswith("```"):
        text = text.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    try:
        plans = json.loads(text)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"规划节点未返回有效 JSON：{exc.msg}") from exc
    if not isinstance(plans, list) or not plans:
        raise RuntimeError("规划节点未生成有效章节计划")
    normalized = []
    for item in plans[:12]:
        if not isinstance(item, dict) or not item.get("title"):
            continue
        generation_prompt = normalize_model_text(item.get("prompt")) or f"请根据用户核心要求起草《{item['title']}》章节。\n用户要求：{requirements}"
        normalized.append({
            "title": str(item["title"])[:200],
            "description": normalize_model_text(item.get("description", item.get("plan", ""))),
            "knowledge_points": normalize_model_text(item.get("knowledge_points", "")),
            "plan": normalize_model_text(item.get("plan", "")),
            "check_criteria": normalize_model_text(item.get("check_criteria", "")),
            "prompt": long_prompt(generation_prompt),
            "source_text": verified_excerpt(str(item.get("source_text", "")), tender_text, str(item["title"]), str(item.get("knowledge_points", ""))),
            "reference_text": verified_excerpt(str(item.get("reference_text", "")), reference_text, str(item["title"]), str(item.get("knowledge_points", ""))),
            "model": chapter_model,
        })
    if not normalized:
        raise RuntimeError("规划结果缺少章节标题")
    cover_index = next((i for i, item in enumerate(normalized) if "封面" in item["title"]), None)
    if cover_index is None:
        normalized.insert(0, cover_plan(contract_title, contract_number, tender_text, reference_text, chapter_model))
    else:
        cover = normalized.pop(cover_index)
        default_cover = cover_plan(contract_title, contract_number, tender_text, reference_text, chapter_model)
        cover.update({"title": "封面", "description": cover.get("description") or default_cover["description"],
                      "knowledge_points": cover.get("knowledge_points") or default_cover["knowledge_points"],
                      "plan": cover.get("plan") or default_cover["plan"],
                      "check_criteria": cover.get("check_criteria") or default_cover["check_criteria"],
                      "prompt": long_prompt(cover.get("prompt") or default_cover["prompt"]),
                      "source_text": cover.get("source_text") or default_cover["source_text"],
                      "reference_text": cover.get("reference_text") or default_cover["reference_text"]})
        normalized.insert(0, cover)
    overview = opening_overview_plan(tender_text, chapter_model)
    if overview:
        existing_overview_index = next((index for index, item in enumerate(normalized)
                                        if item["title"].strip() == OPENING_OVERVIEW_TITLE), None)
        if existing_overview_index is None:
            normalized.insert(1, overview)
        else:
            normalized[existing_overview_index] = overview
    return normalized[:12]

class State(TypedDict, total=False):
    requirements: str
    node_models: dict
    contract_title: str
    contract_number: str
    tender_text: str
    reference_text: str
    tender_compact_text: str
    reference_compact_text: str
    plans: list[dict]
    generated: Annotated[list[dict], operator.add]
    completed_titles: list[str]
    chapter: dict
    final_text: str
    retry_callback: Callable[[str, int, int, int], None]

def plan_node(state: State):
    model = state["node_models"].get("planner", DEFAULT_MODEL)
    tender = state.get("tender_text", "")
    reference = state.get("reference_text", "")
    tender_for_prompt = state.get("tender_compact_text") or compact_document_text(tender)
    reference_for_prompt = state.get("reference_compact_text") or compact_document_text(reference, 18000)
    has_reference = bool(reference_for_prompt.strip())
    if has_reference:
        planner_prompt = PLANNER_PROMPT
        reference_input = f"\n\n参考文档压缩文本（只用于对应章节的结构与文风）：\n{reference_for_prompt}"
    else:
        planner_prompt = PLANNER_NO_REFERENCE_PROMPT
        reference_input = "\n\n本项目未上传参考文档；请直接按标书拆分，所有阶段的 reference_text 留空。"
    prompt = (f"合同名称：{state.get('contract_title', '合同')}\n合同编号：{state.get('contract_number', '【待补充】')}\n"
              f"用户补充要求：\n{state['requirements']}\n\n标书压缩文本（优先以此理解并拆分合同要求）：\n{tender_for_prompt or '未上传标书'}"
              f"{reference_input}")
    raw = ask(planner_prompt, prompt, model, state.get("retry_callback"),
              node_key="planner", phase="需求拆解")
    return {"plans": parse_plans(raw, state["requirements"], state["node_models"].get("chapter_default", DEFAULT_MODEL),
                                  state.get("contract_title", "合同"), state.get("contract_number", "【待补充】"), tender, reference)}

def fanout(state: State):
    completed = set(state.get("completed_titles", []))
    return [Send("generate", {"requirements": state["requirements"],
                               "contract_title": state.get("contract_title", "合同"),
                               "contract_number": state.get("contract_number", "【待补充】"),
                               "chapter": ch,
                               "retry_callback": state.get("retry_callback")})
            for ch in state["plans"] if ch["title"] not in completed]

def start_route(state: State):
    if not state.get("plans"):
        return "plan"
    completed = set(state.get("completed_titles", []))
    if all(chapter["title"] in completed for chapter in state["plans"]):
        return "merge"
    return fanout(state)

def generate_node(state: State):
    chapter = state["chapter"]
    model = chapter.get("model", DEFAULT_MODEL)
    if chapter.get("title") == OPENING_OVERVIEW_TITLE:
        content = opening_overview_excerpt(chapter.get("source_text", "")) or chapter.get("source_text", "")
        return {"generated": [{"title": chapter["title"], "content": content,
                                "self_check": "已按标书原文直接保留开标一览表；未改写单元格内容。",
                                "model": model}]}
    chapter_input = (f"合同名称：{state.get('contract_title', '合同')}\n合同编号：{state.get('contract_number', '【待补充】')}\n"
                     f"合同核心要求：\n{state.get('requirements', '')}\n\n阶段描述：\n{chapter.get('description', '')}\n\n"
                     f"对应标书文本：\n{chapter.get('source_text', '') or '无单独匹配摘录'}\n\n"
                     f"本阶段对应参考文本：\n{chapter.get('reference_text', '') or '未提供对应参考段落'}\n\n"
                     f"默认/自定义提示词：\n{chapter['prompt']}\n\n"
                     "起草时以标书和本项目要求为事实依据；参考文本只参照行文风格、条款结构和层级，不复制参考项目事实。")
    callback_key = f"chapter_title:{chapter['title']}"
    content = ask(CHAPTER_SYSTEM_PROMPT, chapter_input, model, state.get("retry_callback"),
                  node_key=callback_key, phase="章节正文生成")
    review_input = (f"原始要求：\n{state['requirements']}\n\n章节计划：\n{chapter['plan']}\n\n"
                    f"检查标准：\n{chapter['check_criteria']}\n\n待检查文本：\n{content}")
    self_check = ask(REVIEW_SYSTEM_PROMPT, review_input, model, state.get("retry_callback"),
                     node_key=callback_key, phase="节点自查")
    return {"generated": [{"title": chapter["title"], "content": content,
                            "self_check": self_check, "model": model}]}

def merge_node(state: State):
    ordered = {item["title"]: item for item in state.get("generated", [])}
    sections = []
    for plan in state["plans"]:
        generated = ordered.get(plan["title"])
        if not generated:
            raise RuntimeError(f"章节节点未完成，停止合并：{plan['title']}")
        sections.append(f"{plan['title']}\n\n{generated['content']}")
    return {"final_text": "\n\n".join(sections)}

builder = StateGraph(State)
builder.add_node("plan", plan_node)
builder.add_node("generate", generate_node)
builder.add_node("merge", merge_node)
builder.add_conditional_edges(START, start_route, ["plan", "generate", "merge"])
builder.add_conditional_edges("plan", fanout, ["generate"])
builder.add_edge("generate", "merge")
builder.add_edge("merge", END)
workflow = builder.compile()

chapter_builder = StateGraph(State)
chapter_builder.add_node("generate", generate_node)
chapter_builder.add_edge(START, "generate")
chapter_builder.add_edge("generate", END)
chapter_workflow = chapter_builder.compile()
