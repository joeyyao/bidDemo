<script setup>
import { ref, computed, onMounted, onUnmounted, nextTick, watch } from 'vue'
import { showToast, showConfirmDialog } from 'vant'
import { FileText, Plus, LogOut, ArrowLeft, ChevronRight, Sparkles, Download, History, RefreshCw, CheckCircle2, Circle, LoaderCircle, Pencil, ShieldCheck, GripVertical, Upload, Trash2, Archive, Square, Play, Check, X, Save, Heading1, Heading2, AlignLeft, LayoutGrid, List } from 'lucide-vue-next'
import api from './api'
import TenderTable from './TenderTable.vue'
import { contentBlocks, previewHeading, restoreDraftTables } from './documentBlocks'

const token = ref(localStorage.getItem('contract_token') || '')
const username = ref(localStorage.getItem('contract_user') || '')
const mode = ref('login'), loading = ref(false), mergeLoading = ref(false), restartLoading = ref(false), stopLoading = ref(false), continueLoading = ref(false), contracts = ref([]), selected = ref(null), versions = ref([]), showTrash = ref(false)
const recycleBin = ref({ projects: [], chapters: [] })
const modelOptions = ['qwen3.8-flash', 'qwen3.8-max']
const form = ref({ username: '', password: '' }), newForm = ref({ title: '', contract_number: '', requirements: '', planner_model: 'qwen3.8-flash', chapter_model: 'qwen3.8-flash' })
const newTenderFile = ref(null), newReferenceFiles = ref([])
const tenderSourceInput = ref(null), restoringTenderSource = ref(false)
const showCreate = ref(false), showHistory = ref(false), showAddStage = ref(false), editing = ref(null), promptDraft = ref('')
const stageForm = ref({ title: '', description: '' })
const draggedChapterId = ref(null)
const collapsedChapterIds = ref([])
const editingTitleId = ref(null), titleDraft = ref('')
const detailTab = ref('content')
const projectListView = ref('cards')
const chapterRetrying = ref([])
const addingStage = ref(false)
const planningTextPreview = ref(null)
const editorOpen = ref(false), finalDocument = ref(null), editorSaving = ref(false), editorSavedAt = ref(''), editorDirty = ref(false), activeEditorSection = ref(0)
const tenderFormatNotice = ref('')
const editorOpening = ref(false)
const tablePreview = ref(null)
const tablePreviewLoading = ref(false)
let poller
const busy = computed(() => selected.value && ['queued','planning','generating'].includes(selected.value.status))
const canContinue = computed(() => !!selected.value && (selected.value.status === 'paused' || selected.value.status === 'failed' || (!busy.value && (selected.value.chapters.some(ch => ch.status !== 'completed') || (selected.value.chapters.length > 0 && !selected.value.final_text)))))
const allChaptersCollapsed = computed(() => !!selected.value?.chapters?.length && selected.value.chapters.every(ch => collapsedChapterIds.value.includes(ch.id)))
const recentEvents = computed(() => [...(selected.value?.events || [])].reverse())
const selectedReferenceFiles = computed(() => selected.value?.reference_filenames?.length
  ? selected.value.reference_filenames
  : (selected.value?.reference_filename || '').split(/\r?\n/).filter(Boolean))
const activeStreamingEvent = computed(() => {
  if (!busy.value && !chapterRetrying.value.length && !addingStage.value) return null
  const seen = new Set()
  for (const event of recentEvents.value) {
    if (seen.has(event.node_key)) continue
    seen.add(event.node_key)
    if (event.status === 'streaming') return event
  }
  return null
})
let lastStreamingToastAt = 0
watch(() => activeStreamingEvent.value?.id, id => {
  if (!id) return
  const now = Date.now()
  if (now - lastStreamingToastAt < 10000) return
  lastStreamingToastAt = now
  showToast(`${activeStreamingEvent.value?.node_name || 'LangGraph 节点'}：接口正在返回`)
})
const chapterProgress = computed(() => ({
  completed: selected.value?.chapters?.filter(ch => ch.status === 'completed').length || 0,
  total: selected.value?.chapters?.length || 0,
}))
const planningSteps = computed(() => {
  const project = selected.value
  if (!project) return []
  const plannerEvent = [...(project.events || [])].reverse().find(event => event.node_key === 'planner')
  const planningState = plannerEvent?.status === 'completed' ? 'completed' : project.status === 'planning' ? 'running' : 'pending'
  const planningDetail = plannerEvent?.status === 'streaming' ? '模型接口正在返回拆解结果' : planningState === 'completed' ? '阶段计划已生成' : 'LangGraph 正在按合同要求拆分阶段'
  return [
    { title: '标书文本解析', state: project.tender_filename ? 'completed' : 'skipped', detail: project.tender_filename ? '已读取保存的标书文本' : '未上传，已跳过' },
    { title: '参考合同解析', state: project.reference_filename ? 'completed' : 'skipped', detail: project.reference_filename ? '已读取保存的参考合同文本' : '未上传，已跳过' },
    { title: '合同结构拆分', state: planningState, detail: planningDetail },
    { title: '参考章节匹配', state: planningState, detail: planningState === 'completed' ? '已完成对应章节匹配' : '正在匹配参考合同的章节结构与行文风格' },
  ]
})
const gatewayRetryEvent = computed(() => [...(selected.value?.events || [])].reverse().find(event => event.node_key === 'gateway_retry'))
const stateLabel = { queued:'排队中', planning:'制定计划', generating:'生成中', streaming:'接口正在返回', paused:'已停止', completed:'已完成', pending:'待生成', failed:'生成失败' }
const formatProjectDate = value => new Date(value).toLocaleString('zh-CN', { month:'2-digit', day:'2-digit', hour:'2-digit', minute:'2-digit' })
const formatVersionDate = value => new Date(value).toLocaleString('zh-CN')
const formatEventDate = value => new Date(value).toLocaleString('zh-CN', { hour:'2-digit', minute:'2-digit', second:'2-digit' })
const formatTextBytes = value => { const bytes = new TextEncoder().encode(value || '').length; return bytes < 1024 ? `${bytes} B` : `${(bytes / 1024).toFixed(1)} KB` }
function viewPlanningText(kind){
  if(kind==='tender')return showTenderTables(true)
  const content=selected.value?.reference_compact_text
  if(!content)return showToast('没有可查看的参考文本')
  planningTextPreview.value={title:'参考文档文本',content,description:'该压缩文本用于 LangGraph 匹配阶段结构与行文风格。'}
}
async function readTenderLayout(id,showSplitText=false){
  try{
    const params=showSplitText?{split_preview:true}:undefined
    return (await api.get(`/contracts/${id}/tender-layout`,{params})).data
  }catch(e){
    return {supported:false,tables:[],reason:e.message}
  }
}
async function showTenderTables(showSplitText=false){
  const id=selected.value?.id
  if(!id||tablePreviewLoading.value)return
  tablePreview.value={id,showSplitText,layout:null}
  tablePreviewLoading.value=true
  try{
    const layout=await readTenderLayout(id,showSplitText)
    if(tablePreview.value?.id===id)tablePreview.value.layout=layout
  }finally{tablePreviewLoading.value=false}
}
async function viewTenderSource(){
  const project=selected.value
  if(!project)return
  if(!project.tender_source_available){tenderSourceInput.value?.click();return}
  try{
    const {data}=await api.get(`/contracts/${project.id}/tender-source`,{responseType:'blob'})
    const url=URL.createObjectURL(data)
    const link=document.createElement('a')
    link.href=url
    link.download=project.tender_source_filename||project.tender_filename||'tender.docx'
    link.click()
    setTimeout(()=>URL.revokeObjectURL(url),1000)
  }catch(e){showToast(e.message)}
}
async function restoreTenderSource(event){
  const file=event.target.files?.[0]
  event.target.value=''
  const id=selected.value?.id
  if(!file||!id||restoringTenderSource.value)return
  const payload=new FormData()
  payload.append('tender_file',file)
  restoringTenderSource.value=true
  try{
    const {data:layout}=await api.put(`/contracts/${id}/tender-source`,payload)
    if(selected.value?.id!==id)return
    selected.value.tender_source_available=true
    selected.value.tender_source_filename=layout.filename
    const preview=tablePreview.value
    if(preview?.id===id){
      let splitBlocks=preview.layout?.split_blocks
      if(preview.showSplitText&&splitBlocks==null)splitBlocks=(await readTenderLayout(id,true)).split_blocks
      if(tablePreview.value===preview)preview.layout={...layout,split_blocks:splitBlocks}
    }
    if(editorOpen.value){
      syncEditorDom()
      applyTenderLayout(finalDocument.value,layout,true)
      activeEditorSection.value=0
      const index=finalDocument.value.sections.findIndex(section=>section.source_material)
      if(index>=0)jumpToEditor(index)
    }
    showToast(layout.tables.length?`已恢复 ${layout.tables.length} 张原始表格`:'DOCX 已保存，文档正文中没有表格')
  }catch(e){showToast(e.message)}finally{restoringTenderSource.value=false}
}
function viewAdditionalRequirements(){const content=selected.value?.requirements?.trim();if(!content)return showToast('当前项目没有填写额外要求');planningTextPreview.value={title:'额外要求',content,description:'这些补充要求会作为 LangGraph 需求拆分和合同起草的依据。'}}
const statusTone = s => ['completed'].includes(s) ? 'success' : ['failed'].includes(s) ? 'danger' : ['queued','planning','generating','streaming'].includes(s) ? 'progress' : 'neutral'
function chapterDisplayStatus(ch){const event=[...(selected.value?.events||[])].reverse().find(item=>item.node_key===`chapter:${ch.id}`);return event?.status==='streaming'&&(busy.value||chapterRetrying.value.includes(ch.id))?'streaming':ch.status}
function chapterStatusLabel(ch){const status=chapterDisplayStatus(ch);return stateLabel[status]||'待生成'}
async function auth() {
  loading.value = true
  try { const { data } = await api.post(`/auth/${mode.value}`, form.value); token.value=data.access_token; username.value=data.username; localStorage.setItem('contract_token',token.value); localStorage.setItem('contract_user',username.value); await loadContracts() }
  catch(e) { showToast(e.message) } finally { loading.value=false }
}
function logout(){ localStorage.removeItem('contract_token'); localStorage.removeItem('contract_user'); token.value=''; username.value=''; selected.value=null }
async function loadContracts(){ try { contracts.value=(await api.get('/contracts')).data } catch(e){ showToast(e.message) } }
async function openTrash(){try{recycleBin.value=(await api.get('/recycle-bin')).data;showTrash.value=true}catch(e){showToast(e.message)}}
async function deleteProject(){const id=selected.value?.id;if(!id)return;try{await showConfirmDialog({title:'将项目移入回收站？',message:'项目及其阶段会保留在回收站中，可随时恢复。'});await api.delete(`/contracts/${id}`);selected.value=null;clearInterval(poller);await loadContracts();showToast('项目已移入回收站')}catch(e){if(e!=='cancel')showToast(e.message)}}
async function deleteStage(ch){try{await showConfirmDialog({title:'将阶段移入回收站？',message:`“${ch.title}”的内容和历史版本会保留，可从回收站恢复。`});await api.delete(`/contracts/${selected.value.id}/chapters/${ch.id}`);await openContract(selected.value.id);showToast('阶段已移入回收站')}catch(e){if(e!=='cancel')showToast(e.message)}}
async function restoreProject(project){try{await api.post(`/recycle-bin/contracts/${project.id}/restore`);await openTrash();await loadContracts();showToast('项目已恢复')}catch(e){showToast(e.message)}}
async function restoreStage(stage){try{await api.post(`/recycle-bin/contracts/${stage.contract_id}/chapters/${stage.id}/restore`);await openTrash();await loadContracts();if(selected.value?.id===stage.contract_id)await openContract(stage.contract_id);showToast('阶段已恢复到列表末尾')}catch(e){showToast(e.message)}}
async function openContract(id){ try { selected.value=(await api.get(`/contracts/${id}`)).data; collapsedChapterIds.value=[]; editingTitleId.value=null; detailTab.value='content'; if(busy.value) startPolling() } catch(e){ showToast(e.message) } }
function startPolling(keepWhileIdle=false){ clearInterval(poller); poller=setInterval(async()=>{ if(!selected.value) return; try { selected.value=(await api.get(`/contracts/${selected.value.id}`)).data; await loadContracts(); if(!busy.value&&!keepWhileIdle) clearInterval(poller) } catch{} }, 1800) }
function back(){ selected.value=null; clearInterval(poller); loadContracts() }
async function backToProjects(){
  if(editorOpen.value&&editorDirty.value){
    try{await showConfirmDialog({title:'有未保存的编辑',message:'返回项目列表会丢弃当前未保存的修改。是否继续？'})}
    catch{return}
  }
  editorOpen.value=false
  back()
}
function chooseDocument(kind,event){const file=event.target.files?.[0]||null;if(kind==='tender')newTenderFile.value=file;event.target.value=''}
function chooseReferenceDocuments(event){const files=Array.from(event.target.files||[]);const known=new Set(newReferenceFiles.value.map(file=>`${file.name}:${file.size}:${file.lastModified}`));const additions=[];for(const file of files){const key=`${file.name}:${file.size}:${file.lastModified}`;if(!known.has(key)){known.add(key);additions.push(file)}}newReferenceFiles.value=[...newReferenceFiles.value,...additions];event.target.value=''}
function removeReferenceFile(index){newReferenceFiles.value=newReferenceFiles.value.filter((_,i)=>i!==index)}
async function createContract(){ if(!newForm.value.title || !newForm.value.contract_number) return showToast('请填写合同名称和合同编号'); if(newForm.value.requirements.trim().length<10&&!newTenderFile.value) return showToast('请填写合同要求或上传标书'); loading.value=true; try { const payload=new FormData(); Object.entries(newForm.value).forEach(([key,value])=>payload.append(key,value)); if(newTenderFile.value)payload.append('tender_file',newTenderFile.value); newReferenceFiles.value.forEach(file=>payload.append('reference_files',file)); const {data}=await api.post('/contracts',payload); showCreate.value=false; newForm.value={title:'',contract_number:'',requirements:'',planner_model:'qwen3.8-flash',chapter_model:'qwen3.8-flash'}; newTenderFile.value=null; newReferenceFiles.value=[]; await openContract(data.id); detailTab.value='trace'; startPolling() } catch(e){showToast(e.message)} finally{loading.value=false} }
async function regenerate(){ try { await api.post(`/contracts/${selected.value.id}/generate`); selected.value.status='queued'; selected.value.final_text='';selected.value.final_document=null;  selected.value.chapters.forEach(ch=>{ch.status='pending';ch.content='';ch.self_check=''}); detailTab.value='trace'; startPolling(); showToast('已开始生成') } catch(e){showToast(e.message)} }
async function restartAll(){if(!selected.value)return;try{await showConfirmDialog({title:'重启所有流程？',message:'所有阶段的当前正文和自查结果会清空并重新生成；历史版本会保留，当前阶段计划和顺序不变。'});restartLoading.value=true;const contractId=selected.value.id;await api.post(`/contracts/${contractId}/restart`);selected.value=(await api.get(`/contracts/${contractId}`)).data;selected.value.status='queued';selected.value.final_text='';selected.value.final_document=null;selected.value.chapters.forEach(ch=>{ch.status='pending';ch.content='';ch.self_check=''});detailTab.value='trace';startPolling();showToast('已重启所有阶段生成、自查和全文合并流程')}catch(e){if(e!=='cancel')showToast(e.message)}finally{restartLoading.value=false}}
async function stopWorkflow(){if(!selected.value)return;stopLoading.value=true;try{await api.post(`/contracts/${selected.value.id}/stop`);selected.value.status='paused';selected.value.chapters.forEach(ch=>{if(ch.status==='generating')ch.status='pending'});clearInterval(poller);detailTab.value='trace';showToast('流程已停止，已完成节点保留')}catch(e){showToast(e.message)}finally{stopLoading.value=false}}
async function continueWorkflow(){if(!selected.value)return;continueLoading.value=true;try{await api.post(`/contracts/${selected.value.id}/continue`);selected.value.status='queued';selected.value.final_text='';selected.value.final_document=null;detailTab.value='trace';startPolling();showToast('已继续生成，已完成节点保留，其余节点将重新执行')}catch(e){showToast(e.message)}finally{continueLoading.value=false}}
function closeActionMenu(event){const menu=event.currentTarget.closest('details');if(menu)menu.open=false}
async function mergeNow(){if(!selected.value?.chapters?.length)return showToast('尚无阶段计划，暂时无法合并文本');mergeLoading.value=true;try{await api.post(`/contracts/${selected.value.id}/merge-now`);await openContract(selected.value.id);detailTab.value='final';await openFinalEditor();showToast('最终文本已按阶段顺序合并，可以开始编辑')}catch(e){showToast(e.message)}finally{mergeLoading.value=false}}
function editPrompt(ch){ editing.value=ch; promptDraft.value=ch.prompt }
function toggleChapter(chapterId){collapsedChapterIds.value=collapsedChapterIds.value.includes(chapterId)?collapsedChapterIds.value.filter(id=>id!==chapterId):[...collapsedChapterIds.value,chapterId]}
function toggleAllChapters(){collapsedChapterIds.value=allChaptersCollapsed.value?[]:(selected.value?.chapters||[]).map(ch=>ch.id)}
async function beginTitleEdit(ch){editingTitleId.value=ch.id;titleDraft.value=ch.title;await nextTick();const input=document.getElementById(`stage-title-${ch.id}`);input?.focus();input?.select()}
function cancelTitleEdit(){editingTitleId.value=null;titleDraft.value=''}
async function saveChapterTitle(ch){const title=titleDraft.value.trim();if(!title)return showToast('阶段标题不能为空');if(title===ch.title){cancelTitleEdit();return}try{const {data}=await api.patch(`/contracts/${selected.value.id}/chapters/${ch.id}/title`,{title});ch.title=data.title;selected.value.final_text='';selected.value.final_document=null;if(selected.value.status==='completed')selected.value.status='pending';cancelTitleEdit();showToast('阶段标题已保存')}catch(e){showToast(e.message)}}
async function updateChapterModel(ch, model){ const old=ch.model; ch.model=model; try { await api.patch(`/contracts/${selected.value.id}/chapters/${ch.id}/model`,{model}); showToast('章节模型已更新') } catch(e){ch.model=old;showToast(e.message)} }
async function updateStageModel(stage, model){ try { const {data}=await api.patch(`/contracts/${selected.value.id}/models`,{stage,model}); selected.value.node_models=data.node_models; showToast('节点模型已更新') } catch(e){showToast(e.message)} }
function beginChapterDrag(ch,event){draggedChapterId.value=ch.id;if(event.dataTransfer){event.dataTransfer.effectAllowed='move';event.dataTransfer.setData('text/plain',String(ch.id))}}
async function dropChapter(target){const draggedId=draggedChapterId.value;draggedChapterId.value=null;if(!draggedId||draggedId===target.id)return;const chapters=[...selected.value.chapters];const from=chapters.findIndex(ch=>ch.id===draggedId),to=chapters.findIndex(ch=>ch.id===target.id);if(from<0||to<0)return;const [moved]=chapters.splice(from,1);chapters.splice(to,0,moved);chapters.forEach((ch,index)=>ch.order=index+1);selected.value.chapters=chapters;selected.value.status='pending';selected.value.final_text='';selected.value.final_document=null;try{await api.put(`/contracts/${selected.value.id}/chapters/order`,{chapter_ids:chapters.map(ch=>ch.id)});showToast('阶段顺序已保存')}catch(e){showToast(e.message);await openContract(selected.value.id)}}
async function addStage(){if(!stageForm.value.title.trim())return showToast('请填写阶段名称');if(!stageForm.value.description.trim())return showToast('请填写阶段描述');const contractId=selected.value.id;const requestedTitle=stageForm.value.title.trim();addingStage.value=true;startPolling(true);try{await api.post(`/contracts/${contractId}/chapters`,stageForm.value);stageForm.value={title:'',description:''};showAddStage.value=false;await openContract(contractId);showToast('阶段已新增')}catch(e){if(e.message.includes('已存在')){try{selected.value=(await api.get(`/contracts/${contractId}`)).data;detailTab.value='content';showAddStage.value=false;await nextTick();const existing=selected.value.chapters.find(ch=>ch.title.trim()===requestedTitle);if(existing)document.getElementById(`chapter-card-${existing.id}`)?.scrollIntoView({behavior:'smooth',block:'center'});showToast('阶段名称已存在，已定位到列表中的现有阶段')}catch{showToast(e.message)}}else showToast(e.message)}finally{clearInterval(poller);addingStage.value=false}}
async function savePrompt(){ try { await api.patch(`/contracts/${selected.value.id}/chapters/${editing.value.id}/prompt`,{prompt:promptDraft.value}); editing.value=null; await openContract(selected.value.id); showToast('提示词已保存') } catch(e){showToast(e.message)} }
async function generateChapter(ch){ if(chapterRetrying.value.includes(ch.id))return;chapterRetrying.value=[...chapterRetrying.value,ch.id];const contractId=selected.value.id;ch.status='generating';startPolling(true);try{await api.post(`/contracts/${contractId}/chapters/${ch.id}/generate`);await openContract(contractId);showToast('章节已重新生成；流程已暂停，可继续生成其余节点')}catch(e){ch.status='failed';showToast(e.message)}finally{clearInterval(poller);chapterRetrying.value=chapterRetrying.value.filter(id=>id!==ch.id)} }
async function showVersions(){ try { versions.value=(await api.get(`/contracts/${selected.value.id}/versions`)).data; showHistory.value=true } catch(e){showToast(e.message)} }
function splitKnowledgePoints(value){return (value||'').split(/[，,、；;\n]/).map(point=>point.trim()).filter(Boolean)}
function renderTextHtml(value){
  const escapeHtml=text=>String(text||'').replace(/[&<>"']/g,char=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[char]))
  const lines=String(value||'').split(/\r?\n/),out=[],tables=[]
  const renderRows=rows=>`<table class="html-source-table"><tbody>${rows.map(row=>`<tr>${row.map(cell=>`<td>${escapeHtml(cell)}</td>`).join('')}</tr>`).join('')}</tbody></table>`
  for(const line of lines){
    const start=line.match(/^\[\[TABLE_(\d+)_START\]\]$/),end=line.match(/^\[\[TABLE_(\d+)_END\]\]$/)
    if(start){tables.push({id:start[1],rows:[]});continue}
    const table=tables[tables.length-1]
    if(end&&table?.id===end[1]){out.push(renderRows(tables.pop().rows));continue}
    const row=line.match(/^表格 \d+ 第 \d+ 行：(.*)$/)
    if(table&&row){table.rows.push(row[1].split(' | '));continue}
    if(table){table.rows.push([line]);continue}
    if(!line.trim())continue
    const safe=escapeHtml(line.trim())
    out.push(previewHeading.test(line.trim())?`<h4>${safe}</h4>`:`<p>${safe}</p>`)
  }
  for(const table of tables.reverse())out.push(renderRows(table.rows))
  return out.join('')
}
function applyTenderLayout(draft,layout,preserveEdits){
  draft.sections=draft.sections.filter(section=>!section.preview_only)
  if(!layout.supported){
    const count=layout.tables.length
    if(count && !draft.sections.some(section=>section.source_material)){
      const blocks=layout.tables.map(table=>({...table,kind:'table'}))
      draft.sections.push({title:'标书表格参考',source_material:true,preview_only:true,blocks})
    }
    tenderFormatNotice.value=count?`编辑区已显示从旧项目文字识别的 ${count} 组表格。补传 DOCX 可恢复原始排版。`:layout.reason
    return
  }
  const consistent=!draft.tender_layout_revision||draft.tender_layout_revision===layout.revision
  const existing=draft.sections.find(section=>section.source_material)
  const savedTables=new Map((consistent?existing?.blocks||[]:[]).map(block=>[block.source_table_index,block]))
  const blocks=layout.tables.map(table=>{
    const oldCells=new Map((savedTables.get(table.table_index)?.rows||[]).flat().map(cell=>[cell.source_cell_index,cell]))
    for(const cell of table.rows.flat()){
      const old=oldCells.get(cell.source_cell_index)
      if(old&&old.original_text===cell.original_text)cell.text=old.text
    }
    return {...table,kind:'table',source_table_index:table.table_index}
  })
  for(const field of ['header','footer','page_number_format']){
    const templateField=`template_${field}`
    if(!preserveEdits || draft[field]===draft[templateField])draft[field]=layout[field]
    draft[templateField]=layout[field]
  }
  draft.tender_layout_revision=layout.revision
  draft.sections=draft.sections.filter(section=>!section.source_material)
  if(blocks.length)draft.sections.push({title:'标书原始表格',source_material:true,blocks})
  tenderFormatNotice.value=blocks.length?`已加载 ${blocks.length} 张标书原始表格。`:'标书已加载，文档正文中没有表格。'
}
async function openFinalEditor(){
  const project=selected.value
  if(!project||editorOpening.value)return
  editorOpening.value=true
  try{
    const saved=project.final_document
    const draft=saved?.sections?.length?JSON.parse(JSON.stringify(saved)):{
      header:project.document_header||project.title,
      footer:project.document_footer||'',
      page_number_format:project.page_number_format||'第 {page} 页',
      sections:project.chapters.map(ch=>({chapter_id:ch.id,title:ch.title,is_cover:ch.title.includes('封面'),blocks:contentBlocks(ch.content||'【该阶段尚未生成，生成完成后可再次合并】')})),
    }
    draft.cover_title??=project.title
    draft.contract_number??=project.contract_number
    for(const section of draft.sections){
      if(!section.source_material)section.blocks=restoreDraftTables(section.blocks)
    }
    const layout=await readTenderLayout(project.id)
    if(selected.value?.id!==project.id)return
    applyTenderLayout(draft,layout,!!saved)
    finalDocument.value=draft
    editorSavedAt.value=saved?'已保存':'尚未保存'
    activeEditorSection.value=0
    editorOpen.value=true
    await nextTick()
    editorDirty.value=false
  }finally{editorOpening.value=false}
}
watch(finalDocument,()=>{if(editorOpen.value)editorDirty.value=true},{deep:true})
const editorTableIndex = computed(()=>finalDocument.value?.sections.findIndex(section=>section.source_material)??-1)
const hasOriginalEditorTables = computed(()=>editorTableIndex.value>=0&&!finalDocument.value.sections[editorTableIndex.value].preview_only)
function editTableCell(cell,text){cell.text=text.replace(/\u00a0/g,' ')}
function updateEditorBlock(section,block,event){block.text=event.target.innerText.replace(/\u00a0/g,' ')}
function addEditorBlock(kind){const index=activeEditorSection.value,section=finalDocument.value?.sections?.[index];if(!section||section.source_material)return;section.blocks.push({kind,text:''});nextTick(()=>{const node=document.querySelector(`[data-editor-section="${index}"] [data-editor-block="${section.blocks.length-1}"]`);node?.focus()})}
function jumpToEditor(sectionIndex,point=''){
  activeEditorSection.value=sectionIndex;const section=finalDocument.value?.sections?.[sectionIndex];if(!section)return
  nextTick(()=>{const root=document.getElementById(`editor-section-${sectionIndex}`);if(!root)return;let target=null
    if(point){const terms=point.split(/[，,、；;\s]+/).filter(term=>term.length>1);target=[...root.querySelectorAll('[data-editor-block]')].find(node=>terms.some(term=>node.innerText.includes(term)))}
    target ||= root.querySelector('[data-editor-block]') || root;target.scrollIntoView({behavior:'smooth',block:'center'});if(target.isContentEditable)target.focus()})
}
function syncEditorDom(){
  const focusedCell=document.activeElement
  if(focusedCell?.hasAttribute('data-table-editor'))focusedCell.blur()
  for(const [index,section] of (finalDocument.value?.sections||[]).entries()){
    const root=document.getElementById(`editor-section-${index}`)
    if(!root)continue
    root.querySelectorAll('[data-editor-block]').forEach(node=>{
      const block=section.blocks[Number(node.dataset.editorBlock)]
      if(block)block.text=node.innerText.replace(/\u00a0/g,' ')
    })
  }
}
async function saveFinalDocument(){
  if(!selected.value||!finalDocument.value)return false
  syncEditorDom()
  editorSaving.value=true
  try{
    const draft={...finalDocument.value,sections:finalDocument.value.sections.filter(section=>!section.preview_only)}
    const {data}=await api.put(`/contracts/${selected.value.id}/final-document`,draft)
    selected.value.final_document=data.final_document
    selected.value.final_text=data.final_text
    editorSavedAt.value=`已保存 ${new Date(data.saved_at).toLocaleTimeString('zh-CN',{hour:'2-digit',minute:'2-digit'})}`
    editorDirty.value=false
    return true
  }catch(e){showToast(e.message);return false}finally{editorSaving.value=false}
}
async function closeFinalEditor(){if(editorDirty.value){try{await showConfirmDialog({title:'有未保存的编辑',message:'返回项目会丢弃当前未保存的修改。是否继续？'})}catch{return}}editorOpen.value=false}
async function exportDoc(){if(!await saveFinalDocument())return;try{const r=await api.get(`/contracts/${selected.value.id}/export`,{responseType:'blob'});const url=URL.createObjectURL(r.data);const a=document.createElement('a');a.href=url;a.download=`${selected.value.title}.docx`;a.click();URL.revokeObjectURL(url);showToast('Word 文档已导出')}catch(e){showToast(e.message)}}
onMounted(()=>{ if(token.value) loadContracts() }); onUnmounted(()=>clearInterval(poller))
</script>

<template>
  <main class="shell">
    <template v-if="!token">
      <section class="auth-layout">
        <div class="auth-card">
          <div class="auth-brand"><div class="brand-mark"><FileText :size="24"/></div><div><b>smartBidding</b><span>AI CONTRACT WORKFLOW</span></div></div>

          <p>从核心要求出发，生成结构清晰、便于审阅的合同文本。</p>
          <van-form @submit="auth"><van-cell-group inset><van-field v-model="form.username" label="用户名" placeholder="至少 3 个字符" autocomplete="username"/><van-field v-model="form.password" type="password" label="密码" placeholder="至少 6 个字符" autocomplete="current-password"/></van-cell-group><van-button class="primary-btn auth-submit" block round native-type="submit" :loading="loading">{{mode==='login'?'登录':'注册并继续'}}</van-button></van-form>
          <button class="text-button switch-auth" @click="mode=mode==='login'?'register':'login'">{{mode==='login'?'还没有账号？创建账号':'已有账号？返回登录'}} <ChevronRight :size="15"/></button>
        </div><div class="auth-foot">私有工作区 · 每个项目独立保存</div>
      </section>
    </template>
    <template v-else>
      <header class="topbar"><div class="brand"><div class="brand-mark small"><FileText :size="18"/></div><b>smartBidding</b><button v-if="selected&&!editorOpen" class="project-home-link" @click="backToProjects"><ArrowLeft :size="14"/>返回所有项目</button></div><div class="user-area"><span class="user-avatar">{{username.slice(0,1).toUpperCase()}}</span><span class="user-name">{{username}}</span><button class="icon-button" title="退出登录" @click="logout"><LogOut :size="17"/></button></div></header>
      <section v-if="!selected" class="content">
        <div class="page-heading"><div><div class="eyebrow">WORKSPACE / CONTRACTS</div><h1>合同项目</h1><p>管理起草任务，跟踪每个章节的生成进度。</p></div><div class="page-heading-actions"><van-button class="outline-btn" round @click="openTrash"><Archive :size="16"/>回收站</van-button><van-button class="primary-btn" round @click="showCreate=true"><Plus :size="17"/>新建合同</van-button></div></div>
        <div class="stats-row"><div class="stat-card"><span>全部项目</span><b>{{contracts.length}}</b><small>已创建合同任务</small></div><div class="stat-card"><span>生成中</span><b>{{contracts.filter(c=>['queued','planning','generating'].includes(c.status)).length}}</b><small>正在处理的任务</small></div><div class="stat-card"><span>已完成</span><b>{{contracts.filter(c=>c.status==='completed').length}}</b><small>可查看或导出</small></div></div>
        <div class="section-head"><div><h2>项目列表</h2><span>{{contracts.length}} 个项目</span></div><div class="project-list-actions"><div class="view-toggle" role="group" aria-label="项目显示方式"><button :class="{active:projectListView==='cards'}" :aria-pressed="projectListView==='cards'" title="卡片视图" @click="projectListView='cards'"><LayoutGrid :size="15"/><span>卡片</span></button><button :class="{active:projectListView==='table'}" :aria-pressed="projectListView==='table'" title="表格视图" @click="projectListView='table'"><List :size="15"/><span>表格</span></button></div><button class="text-button" @click="loadContracts"><RefreshCw :size="14"/>刷新</button></div></div>
        <template v-if="contracts.length"><div v-if="projectListView==='cards'" class="project-grid"><button v-for="c in contracts" :key="c.id" class="project-card" @click="openContract(c.id)"><div class="project-icon"><FileText :size="20"/></div><div class="project-title-row"><h3>{{c.title}}</h3><span class="status-pill" :class="statusTone(c.status)"><i></i>{{stateLabel[c.status]||c.status}}</span></div><p class="project-meta">{{c.contract_number}} <span>·</span> {{c.chapter_count}} 个阶段 <span>·</span> {{formatProjectDate(c.created_at)}}</p><div class="project-bottom"><span>打开项目</span><ChevronRight :size="16"/></div></button></div><div v-else class="project-table-wrap"><table class="project-table"><thead><tr><th>项目名称</th><th>合同编号</th><th>状态</th><th>阶段数</th><th>创建时间</th><th></th></tr></thead><tbody><tr v-for="c in contracts" :key="c.id"><td><button class="project-table-title" @click="openContract(c.id)"><FileText :size="16"/>{{c.title}}</button></td><td>{{c.contract_number||'—'}}</td><td><span class="status-pill" :class="statusTone(c.status)"><i></i>{{stateLabel[c.status]||c.status}}</span></td><td>{{c.chapter_count}}</td><td>{{formatProjectDate(c.created_at)}}</td><td><button class="project-table-open" @click="openContract(c.id)">打开项目 <ChevronRight :size="14"/></button></td></tr></tbody></table></div></template>
        <div v-else class="empty-state"><div class="empty-icon"><FileText :size="25"/></div><h3>开始你的第一个合同项目</h3><p>描述合作背景和核心约定，AI 会为你拆解章节并逐步起草。</p><van-button class="primary-btn" round @click="showCreate=true"><Plus :size="17"/>新建合同</van-button></div>
      </section>
      <section v-else-if="editorOpen" class="final-editor-workspace">
        <div class="editor-header-group">
          <header class="final-editor-bar"><button class="editor-back" @click="closeFinalEditor"><ArrowLeft :size="17"/>返回项目</button><div class="editor-document-name"><b>{{selected.title}}</b><span>合同最终编辑</span></div><span class="editor-save-state">{{editorSaving?'保存中…':editorDirty?'有未保存修改':editorSavedAt}}</span><button class="outline-btn editor-save-button" :disabled="editorSaving" @click="saveFinalDocument"><Save :size="15"/>临时保存</button><button class="primary-btn editor-export-button" :disabled="editorSaving" @click="exportDoc"><Download :size="15"/>导出 Word</button></header>
          <div class="editor-source-status"><div class="source-status-item" :class="{'source-skipped':!selected.tender_filename}"><CheckCircle2 v-if="selected.tender_filename" :size="15"/><Circle v-else :size="15"/><b>标书文本解析</b><span>{{selected.tender_filename?'已读取保存的标书文本':'未上传标书，已跳过'}}</span><em>{{selected.tender_filename?'完成':'跳过'}}</em></div><div class="source-status-item" :class="{'source-skipped':!selected.reference_filename}"><CheckCircle2 v-if="selected.reference_filename" :size="15"/><Circle v-else :size="15"/><b>参考文本解析</b><span>{{selectedReferenceFiles.length?`已读取保存 ${selectedReferenceFiles.length} 个参考文档`:'未上传参考文档，已跳过'}}</span><em>{{selected.reference_filename?'完成':'跳过'}}</em></div><div class="source-format-notice"><b>最终文本格式</b><span>{{tenderFormatNotice}}</span><button class="planning-text-button" @click="editorTableIndex>=0?jumpToEditor(editorTableIndex):showTenderTables()"><FileText :size="14"/>{{editorTableIndex>=0?'定位表格':'预览 / 补传标书表格'}}</button><button v-if="editorTableIndex>=0&&!hasOriginalEditorTables" class="planning-text-button" @click="showTenderTables()"><Upload :size="14"/>补传原始 DOCX</button></div></div>
        </div>
        <div class="final-editor-layout"><aside class="editor-outline"><div class="outline-heading"><div><span class="eyebrow">DOCUMENT OUTLINE</span><h2>目录</h2></div><span>{{finalDocument?.sections?.length||0}} 节</span></div><div class="outline-tree"><div v-for="(section,index) in finalDocument?.sections||[]" :key="section.chapter_id||index" class="outline-group"><button class="outline-section" :class="{active:activeEditorSection===index}" @click="jumpToEditor(index)"><span class="outline-number">{{String(index+1).padStart(2,'0')}}</span><span>{{section.title}}</span></button><button v-for="(point,pointIndex) in splitKnowledgePoints(selected.chapters.find(ch=>ch.id===section.chapter_id)?.knowledge_points)" :key="pointIndex" class="outline-point" @click="jumpToEditor(index,point)"><span class="outline-tree-elbow">└</span>{{point}}</button></div></div></aside>
          <main class="editor-main"><div class="word-toolbar"><span class="word-toolbar-label"><FileText :size="15"/>编辑工具</span><button :disabled="activeEditorSection===editorTableIndex" @click="addEditorBlock('paragraph')"><AlignLeft :size="15"/>正文</button><button :disabled="activeEditorSection===editorTableIndex" @click="addEditorBlock('heading')"><Heading1 :size="15"/>标题 1</button><button :disabled="activeEditorSection===editorTableIndex" @click="addEditorBlock('subheading')"><Heading2 :size="15"/>标题 2</button><span class="word-toolbar-hint">点击文档文字即可编辑；标书表格会按原始结构保留</span></div><div class="editor-scroll"><article class="word-paper"><div class="word-page-content"><section v-for="(section,index) in finalDocument.sections" :id="`editor-section-${index}`" :data-editor-section="index" :key="section.chapter_id||`source-${index}`" class="word-section" :class="{'cover-section':section.is_cover,'source-material-section':section.source_material}" @click="activeEditorSection=index"><div class="word-section-kicker">{{section.preview_only?'从旧项目文字识别的表格 · 只读参考':section.source_material?'标书表格 · 原始 Word':section.is_cover?'封面阶段':`阶段 ${String(index).padStart(2,'0')}`}}</div><template v-if="section.is_cover"><input v-model="finalDocument.cover_title" class="word-section-title cover-title" aria-label="编辑封面合同名称"/><input v-model="finalDocument.contract_number" class="cover-contract-number" aria-label="编辑封面合同编号"/></template><h2 v-else-if="section.source_material" class="word-section-title chapter-title-style">{{section.title}}</h2><input v-else v-model="section.title" class="word-section-title chapter-title-style" aria-label="编辑阶段标题"/><div v-if="!section.blocks.length" class="editor-empty-block">此阶段暂时没有文本，可使用上方工具添加内容。</div><template v-for="(block,blockIndex) in section.blocks" :key="blockIndex"><TenderTable v-if="block.kind==='table'" :table="block" :editable="!section.preview_only" fill-gaps @edit="editTableCell" @input="editorDirty=true" /><div v-else class="word-edit-row"><select v-model="block.kind" aria-label="设置段落样式"><option value="paragraph">正文</option><option value="heading">标题 1</option><option value="subheading">标题 2</option></select><div :data-editor-block="blockIndex" contenteditable="true" spellcheck="false" class="word-edit-block" :class="`word-${block.kind}`" :data-placeholder="block.kind==='paragraph'?'点击输入正文':''" @blur="updateEditorBlock(section,block,$event)" v-text="block.text"></div></div></template></section></div><label class="word-page-footer"><span>页脚</span><input v-model="finalDocument.footer" placeholder="点击输入页脚" aria-label="编辑页脚"/><span class="page-number-setting"><span>页码格式</span><input v-model="finalDocument.page_number_format" placeholder="第 {page} 页" aria-label="编辑页码格式"/></span></label></article><div class="editor-footnote">原始表格按标书中的表格结构、合并单元格与 Word 样式导出；页码格式中使用 <code>{page}</code> 表示页码。</div></div></main></div>
      </section>
      <section v-else class="content detail-content">
        <div class="detail-heading"><div><div class="eyebrow">合同编号 / {{selected.contract_number || String(selected.id).padStart(4,'0')}}</div><h1>{{selected.title}}</h1><p>{{selected.requirements}}</p></div><div class="heading-actions"><span class="status-pill large" :class="statusTone(selected.status)"><i></i>{{stateLabel[selected.status]||selected.status}}</span><details class="action-dropdown" @mouseleave="$event.currentTarget.open=false"><summary>项目操作 <ChevronRight :size="14"/></summary><div class="action-dropdown-panel"><button class="danger-action" @click="deleteProject();closeActionMenu($event)"><Trash2 :size="15"/>删除项目</button></div></details><details class="action-dropdown" @mouseleave="$event.currentTarget.open=false"><summary>流程操作 <ChevronRight :size="14"/></summary><div class="action-dropdown-panel"><button v-if="busy" :disabled="stopLoading" @click="stopWorkflow();closeActionMenu($event)"><Square :size="15"/>停止流程</button><button v-if="canContinue" :disabled="continueLoading" @click="continueWorkflow();closeActionMenu($event)"><Play :size="15"/>继续生成</button><button :disabled="restartLoading" @click="restartAll();closeActionMenu($event)"><RefreshCw :size="15"/>重新开始</button><button :disabled="mergeLoading||!selected.chapters.length" @click="mergeNow();closeActionMenu($event)"><FileText :size="15"/>生成最终文本</button><button v-if="selected.status==='completed'" @click="showVersions();closeActionMenu($event)"><History :size="15"/>历史版本</button></div></details></div></div>
        <div v-if="busy||activeStreamingEvent||chapterRetrying.length||addingStage" class="progress-banner"><div class="progress-spinner"><LoaderCircle :size="19"/></div><div><b>{{addingStage?'正在生成新增阶段计划':selected.status==='planning'?'正在拆解合同结构':selected.status==='queued'?'任务已加入队列':chapterRetrying.length?'正在重新生成章节':'章节正在逐项生成'}}</b><span>生成过程会自动保存，你可以稍后返回查看。</span></div><div v-if="activeStreamingEvent" class="streaming-output-notice"><LoaderCircle class="spin" :size="13"/><span>{{activeStreamingEvent.node_name}}：接口正在返回</span></div><div v-if="selected.status==='planning'" class="planning-flow"><div v-for="step in planningSteps" :key="step.title" class="planning-flow-step" :class="`flow-${step.state}`"><span class="planning-flow-icon"><CheckCircle2 v-if="step.state==='completed'" :size="14"/><LoaderCircle v-else-if="step.state==='running'" class="spin" :size="14"/><Circle v-else :size="14"/></span><div><b>{{step.title}}</b><small>{{step.detail}}</small></div><em>{{step.state==='completed'?'完成':step.state==='running'?'处理中':step.state==='skipped'?'跳过':'等待'}}</em></div></div><div v-if="gatewayRetryEvent?.status==='running'" class="gateway-retry-notice"><LoaderCircle class="spin" :size="13"/><span>{{gatewayRetryEvent.message}}</span></div><span v-if="selected.chapters.length" class="progress-count">{{selected.chapters.filter(x=>x.status==='completed').length}} / {{selected.chapters.length}} 章</span></div>
        <van-tabs v-model:active="detailTab" class="detail-tabs" animated>
        <van-tab title="内容文本" name="content">
        <div class="detail-grid"><div class="chapter-column"><div class="stage-toolbar"><span>{{selected.chapters.length}} 个阶段 · 拖动节点可调整顺序</span><div class="stage-toolbar-actions"><button class="text-button collapse-all-button" :disabled="!selected.chapters.length" @click="toggleAllChapters">{{allChaptersCollapsed?'展开全部':'收起全部'}}</button><van-button class="outline-btn add-stage-btn" round :disabled="busy" @click="showAddStage=true"><Plus :size="15"/>新增阶段</van-button></div></div>
          <div v-if="!selected.chapters.length" class="stage-empty-placeholder"><div class="stage-placeholder-icon"><LoaderCircle v-if="busy" class="spin" :size="20"/><FileText v-else :size="20"/></div><div class="stage-placeholder-copy"><b>{{busy?'正在整理阶段计划':selected.status==='failed'?'阶段计划生成失败':'当前还没有阶段'}}</b><span>{{busy?'计划生成后会显示在这里':selected.status==='failed'?'可点击“重新开始”重试，或手动新增阶段。':'可点击“重新开始”生成计划，或手动新增阶段。'}}</span></div><span class="stage-placeholder-count">0 个阶段</span></div>
          <article v-for="(ch,i) in selected.chapters" :id="`chapter-card-${ch.id}`" :key="ch.id" class="chapter-card" :class="{'drag-target':draggedChapterId===ch.id}" @dragover.prevent @drop.prevent="dropChapter(ch)"><div class="chapter-top" :draggable="!busy" @dragstart="beginChapterDrag(ch,$event)" @dragend="draggedChapterId=null"><button class="mini-button drag-handle" title="拖动此行调整阶段顺序" :draggable="!busy" :disabled="busy" @dragstart.stop="beginChapterDrag(ch,$event)" @dragend="draggedChapterId=null"><GripVertical :size="15"/></button><div class="chapter-number">{{String(i+1).padStart(2,'0')}}</div><div class="chapter-heading"><div class="chapter-title-wrap"><template v-if="editingTitleId===ch.id"><input :id="`stage-title-${ch.id}`" v-model="titleDraft" class="chapter-title-input" :disabled="busy" aria-label="阶段标题" @click.stop @keyup.enter="saveChapterTitle(ch)" @keyup.esc="cancelTitleEdit"/><button class="mini-button title-save-button" title="保存标题" :disabled="busy" @click="saveChapterTitle(ch)"><Check :size="14"/></button><button class="mini-button" title="取消编辑" @click="cancelTitleEdit"><X :size="14"/></button></template><template v-else><button class="chapter-title-toggle" :aria-expanded="!collapsedChapterIds.includes(ch.id)" @click="toggleChapter(ch.id)"><span class="chapter-title-text">{{ch.title}}</span><ChevronRight :size="15" :class="{'chapter-expanded-icon':!collapsedChapterIds.includes(ch.id)}"/></button><button class="mini-button title-edit-button" title="编辑阶段标题" :disabled="busy || ch.status==='generating'" @click="beginTitleEdit(ch)"><Pencil :size="13"/></button></template></div><span class="status-pill" :class="statusTone(chapterDisplayStatus(ch))"><i></i>{{chapterStatusLabel(ch)}}</span></div><div class="chapter-actions"><button class="mini-button" title="编辑提示词" @click="editPrompt(ch)"><Pencil :size="15"/></button><button class="mini-button rerun" :title="busy?'重试本阶段（当前流程会暂停）':'重试本阶段'" :disabled="chapterRetrying.includes(ch.id)" @click="generateChapter(ch)"><RefreshCw :size="15"/></button><button class="mini-button trash-stage-btn" title="移入回收站" :disabled="busy" @click="deleteStage(ch)"><Trash2 :size="15"/></button></div></div>
            <div v-show="!collapsedChapterIds.includes(ch.id)" class="chapter-body"><div class="chapter-plan"><p v-if="ch.description" class="chapter-description">{{ch.description}}</p><b>生成计划</b><p class="plan-copy">{{ch.plan}}</p><div class="tag-row"><span v-for="(point,j) in ch.knowledge_points.split(/[，,、；;]/).filter(Boolean).slice(0,4)" :key="j">{{point}}</span></div><div class="chapter-model-row"><span>本节点推理模型</span><select :value="ch.model" class="model-select" :disabled="busy || ch.status==='generating'" @change="updateChapterModel(ch,$event.target.value)"><option v-for="model in modelOptions" :key="model" :value="model">{{model}}</option></select></div></div>
            <details v-if="ch.source_text" class="stage-source"><summary>对应标书文本</summary><pre>{{ch.source_text}}</pre></details><details class="stage-source reference-source"><summary>对应参考文本</summary><pre>{{ch.reference_text||'当前阶段没有匹配到参考文档片段，可在提示词中补充对应参考内容。'}}</pre></details>
            <div v-if="ch.content" class="chapter-result"><div class="result-label"><span><CheckCircle2 :size="14"/>章节结果</span><button class="text-button" @click="editPrompt(ch)">调整提示词 <Pencil :size="13"/></button></div><details class="chapter-fold generated-fold"><summary><span><FileText :size="14"/>生成文本</span><small>{{ch.content.length}} 字 · 点击展开</small></summary><div class="folded-text-content html-text-content" v-html="renderTextHtml(ch.content)"></div></details><details v-if="ch.self_check" class="chapter-fold check-fold"><summary><span><ShieldCheck :size="14"/>检查结果</span><small>点击展开</small></summary><pre class="folded-text-content check-result-content">{{ch.self_check}}</pre></details></div><div v-else class="pending-note"><Circle :size="14"/><span>{{ch.status==='failed'?'生成失败':i>0?'生成中':'封面待生成'}}</span></div></div>
          </article>
        </div><aside class="side-column"><div class="side-card"  ><div class="side-title"><Sparkles :size="16"/>项目说明</div><p>{{selected.requirements||'本项目需求主要来自已上传的标书。'}}</p><div v-if="selected.tender_filename||selected.reference_filename" class="document-list"><div v-if="selected.tender_filename"><b>标书文件</b><span>{{selected.tender_filename}}</span></div><div v-for="filename in selectedReferenceFiles" :key="filename"><b>参考文档</b><span>{{filename}}</span></div></div><div class="project-chapter-summary"><span class="count-chip">章节计划 · {{selected.chapters.length}} 章</span><small>逐章查看生成内容与自查结果</small></div></div><div class="side-card process-card"><div class="side-title"><span class="langgraph-badge">LG</span>LangGraph 节点模型</div><div class="stage-model"><span>需求规划节点</span><select class="model-select" :value="selected.node_models?.planner||'qwen3.8-flash'" :disabled="busy" @change="updateStageModel('planner',$event.target.value)"><option v-for="model in modelOptions" :key="model" :value="model">{{model}}</option></select></div><div class="stage-model"><span>章节节点默认模型</span><select class="model-select" :value="selected.node_models?.chapter_default||'qwen3.8-flash'" :disabled="busy" @change="updateStageModel('chapter_default',$event.target.value)"><option v-for="model in modelOptions" :key="model" :value="model">{{model}}</option></select></div></div><div class="side-card tip-card"><div class="tip-mark">i</div><p>每个章节节点可单独选模型。全文合并只会在所有章节生成和自查完成后触发。</p></div></aside></div>
        </van-tab>
        <van-tab name="trace">
          <template #title><span>推理过程</span><span v-if="chapterProgress.total" class="tab-progress-bubble">{{chapterProgress.completed}}/{{chapterProgress.total}}</span></template>
          <section class="trace-section"><div class="trace-heading"><div><div class="eyebrow">WORKFLOW OBSERVABILITY</div><h2>LangGraph 推理与流转</h2><p>记录规划、Send 分发、章节生成与自查，以及全文合并节点的执行状态。</p></div><span class="langgraph-badge large">LangGraph</span></div><div class="planning-inputs-card"><div class="planning-inputs-heading"><div><b>需求拆分输入</b><span>查看 LangGraph 规划节点实际使用的压缩文本</span></div><span class="langgraph-badge">LangGraph</span></div><div class="planning-inputs-grid"><article class="planning-input-item"><div><b>标书文本</b><span>{{selected.tender_filename||'未上传标书'}}</span></div><small>{{selected.tender_compact_text?.length||0}} 字</small><div class="planning-text-actions"><button class="planning-text-button" :disabled="tablePreviewLoading" @click="viewPlanningText('tender')"><FileText :size="14"/>查看拆分文本</button><button class="planning-text-button" :disabled="restoringTenderSource" @click="viewTenderSource"><FileText :size="14"/>{{selected.tender_source_available?'下载源文件':'补传 DOCX'}}</button></div></article><article class="planning-input-item"><div><b>参考文档</b><span>{{selectedReferenceFiles.length?selectedReferenceFiles.join('、'):'未上传参考文档'}}</span></div><small>{{selected.reference_compact_text?.length||0}} 字</small><button class="planning-text-button" :disabled="!selected.reference_compact_text" @click="viewPlanningText('reference')"><FileText :size="14"/>查看参考文本</button></article><article class="planning-input-item"><div><b>额外要求</b><span>{{selected.requirements?.trim()||'未填写额外要求'}}</span></div><small>{{selected.requirements?.length||0}} 字</small><button class="planning-text-button" :disabled="!selected.requirements?.trim()" @click="viewAdditionalRequirements"><FileText :size="14"/>查看额外要求</button></article></div></div><div v-if="recentEvents.length" class="trace-list"><article v-for="event in recentEvents" :key="event.id" class="trace-event" :class="event.status"><span class="trace-dot"></span><div class="trace-main"><div class="trace-title"><b>{{event.node_name}}</b><span class="trace-status">{{event.status==='streaming'?'接口正在返回':event.status==='running'?'执行中':event.status==='failed'?'失败':'完成'}}</span></div><p>{{event.message}}</p><div class="trace-meta"><span v-if="event.model">模型 · {{event.model}}</span><span>{{formatEventDate(event.created_at)}}</span></div></div></article></div><div v-else class="trace-empty">工作流启动后，LangGraph 节点事件会显示在这里。</div></section>
        </van-tab>
        <van-tab name="final">
          <template #title>最终文本</template>
          <div class="final-editor-launch"><div class="final-launch-icon"><FileText :size="22"/></div><div><span class="eyebrow">FINAL DOCUMENT EDITOR</span><h2>合同最终文本工作区</h2><p>在新的编辑界面中按阶段修改合同，使用左侧目录快速定位。页眉、页脚和页码格式可编辑，支持临时保存和导出 Word。</p><div v-if="selected.final_text" class="final-launch-meta">已有合并文本{{selected.final_document?' · 编辑稿已保存':''}}</div><div v-else class="final-launch-meta">{{busy?'可按当前进度先合并已生成的阶段':'阶段生成完成后可生成最终文本'}}</div></div><div class="final-launch-actions"><van-button class="primary-btn" round :loading="mergeLoading||editorOpening" :disabled="!selected.chapters.length" @click="selected.final_text?openFinalEditor():mergeNow()">{{selected.final_text?'打开编辑器':'生成并编辑最终文本'}} <ChevronRight :size="16"/></van-button><button class="text-button" @click="showTenderTables()"><FileText :size="15"/>预览标书表格</button><button v-if="selected.final_text" class="text-button" :disabled="mergeLoading" @click="mergeNow">重新合并阶段内容</button></div></div>
        </van-tab>
        </van-tabs>
      </section>
    </template>
    <van-popup v-model:show="showTrash" position="right" class="drawer history-drawer"><div class="drawer-head"><button class="icon-button" @click="showTrash=false"><ArrowLeft :size="18"/></button><b>回收站</b><span class="history-total">{{recycleBin.projects.length+recycleBin.chapters.length}} 项</span></div><div class="trash-list"><section v-if="recycleBin.projects.length"><h3>已删除项目</h3><article v-for="project in recycleBin.projects" :key="project.id" class="trash-entry"><div><b>{{project.title}}</b><small>{{project.contract_number}} · 删除于 {{formatProjectDate(project.deleted_at)}}</small></div><van-button class="outline-btn" round @click="restoreProject(project)">恢复</van-button></article></section><section v-if="recycleBin.chapters.length"><h3>已删除阶段</h3><article v-for="stage in recycleBin.chapters" :key="stage.id" class="trash-entry"><div><b>{{stage.title}}</b><small>{{stage.contract_title}} · 删除于 {{formatProjectDate(stage.deleted_at)}}</small></div><van-button class="outline-btn" round @click="restoreStage(stage)">恢复</van-button></article></section><div v-if="!recycleBin.projects.length&&!recycleBin.chapters.length" class="trash-empty"><Archive :size="24"/><b>回收站是空的</b><span>删除的项目和阶段会显示在这里。</span></div></div></van-popup>
    <van-popup v-model:show="showCreate" position="right" class="drawer"><div class="drawer-head"><button class="icon-button" @click="showCreate=false"><ArrowLeft :size="18"/></button><b>新建合同</b></div><div class="drawer-body"><div class="eyebrow">NEW PROJECT</div><h2>描述你的合同需求</h2><p class="drawer-intro">上传标书后将优先依据标书拆分阶段；参考文档用于匹配各阶段的结构和行文风格。</p><label>合同名称</label><van-field v-model="newForm.title" placeholder="例如：年度市场推广服务合同" maxlength="100"/><label>合同编号</label><van-field v-model="newForm.contract_number" placeholder="例如：HT-2026-001" maxlength="120"/><label>标书文件（可选）</label><div class="file-picker"><Upload :size="16"/><input type="file" accept=".docx,.pdf,.txt,.md" @change="chooseDocument('tender',$event)"/><span>{{newTenderFile?.name||'选择标书 DOCX / PDF / TXT / MD'}}</span></div><label>参考文档（可选，支持多选）</label><div class="file-picker"><Upload :size="16"/><input type="file" accept=".docx,.pdf,.txt,.md" multiple @change="chooseReferenceDocuments"/><span>{{newReferenceFiles.length ? '已选择 ' + newReferenceFiles.length + ' 个参考文档' : '选择多个参考合同 DOCX / PDF / TXT / MD'}}</span></div><div v-if="newReferenceFiles.length" class="selected-reference-files"><div v-for="(file,index) in newReferenceFiles" :key="file.name+file.size"><span>{{file.name}}</span><button type="button" aria-label="移除参考文档" @click="removeReferenceFile(index)"><X :size="13"/></button></div></div><label>补充要求</label><van-field v-model="newForm.requirements" type="textarea" rows="6" maxlength="10000" show-word-limit placeholder="可填写合同背景、合作目标和标书中未说明的要求。上传标书时，此项可以留空。"/><div class="model-config-group"><label>LangGraph 各阶段推理模型</label><div class="model-config-row"><span>需求规划节点</span><select v-model="newForm.planner_model" class="model-select"><option v-for="model in modelOptions" :key="model">{{model}}</option></select></div><div class="model-config-row"><span>章节节点默认模型</span><select v-model="newForm.chapter_model" class="model-select"><option v-for="model in modelOptions" :key="model">{{model}}</option></select></div></div><div class="form-hint"><Sparkles :size="15"/>模型调用通过配置的阿里云网关完成</div></div><div class="drawer-footer"><div v-if="loading" class="create-loading-flow"><LoaderCircle class="spin" :size="17"/><div><b>{{newTenderFile||newReferenceFiles.length?'正在解析上传文档':'正在准备合同结构解析'}}</b><span>{{newTenderFile&&newReferenceFiles.length?'正在提取标书与参考合同文本…':newTenderFile?'正在提取标书文本…':newReferenceFiles.length?'正在提取参考合同文本…':'合同信息提交后将进入 LangGraph 解析流程'}}</span></div></div><van-button class="primary-btn" block round :loading="loading" @click="createContract">创建并开始生成 <Sparkles :size="16"/></van-button></div></van-popup>
    <van-popup v-model:show="showAddStage" position="center" round class="prompt-sheet stage-sheet"><div class="sheet-title"><div><div class="eyebrow">CUSTOM LANGGRAPH NODE</div><h3>新增合同阶段</h3></div><button class="icon-button" @click="showAddStage=false">×</button></div><p class="prompt-help">填写阶段名称和描述，计划、检查标准、提示词及对应文本由模型自动生成。</p><div class="stage-form-grid"><van-field v-model="stageForm.title" label="阶段名称" placeholder="例如：售后服务"/><van-field v-model="stageForm.description" label="阶段描述" type="textarea" rows="3" autosize placeholder="说明本阶段要覆盖的合同内容和目标"/></div><div class="sheet-actions"><van-button class="outline-btn" round @click="showAddStage=false">取消</van-button><van-button class="primary-btn" round @click="addStage">新增阶段</van-button></div></van-popup>
    <van-popup :show="!!editing" @update:show="v=>!v&&(editing=null)" position="center" round class="prompt-sheet prompt-modal"><div class="sheet-title"><div><div class="eyebrow">CHAPTER PROMPT</div><h3>调整「{{editing?.title}}」提示词</h3></div><button class="icon-button" @click="editing=null">×</button></div><p class="prompt-help">编辑后可保存，并使用章节卡片的重新生成按钮生成新版本。</p><van-field v-model="promptDraft" type="textarea" rows="8" maxlength="10000" show-word-limit class="prompt-input"/><div class="sheet-actions"><van-button class="outline-btn" round @click="editing=null">取消</van-button><van-button class="primary-btn" round @click="savePrompt">保存提示词</van-button></div></van-popup>
    <input ref="tenderSourceInput" class="source-file-input" type="file" accept=".docx,application/vnd.openxmlformats-officedocument.wordprocessingml.document" @change="restoreTenderSource"/>
    <van-popup :show="!!tablePreview" @update:show="open=>!open&&(tablePreview=null)" position="center" round class="prompt-sheet text-preview-modal tender-preview-modal" :class="{'split-preview-modal':tablePreview?.showSplitText}">
      <div class="sheet-title">
        <h3>{{tablePreview?.showSplitText?'标书拆分文本':'标书表格预览'}}</h3>
        <button class="icon-button" aria-label="关闭" @click="tablePreview=null">×</button>
      </div>
      <p v-if="!tablePreview?.layout" class="prompt-help" role="status">正在读取表格…</p>
      <template v-else>
        <div class="tender-preview-actions">
          <p class="prompt-help">
            <template v-if="tablePreview.layout.supported">{{tablePreview.layout.filename}} · {{tablePreview.layout.tables.length}} 张原始表格</template>
            <template v-else>{{tablePreview.layout.reason}}</template>
          </p>
          <button v-if="!tablePreview.layout.supported||!tablePreview.layout.tables.length" class="planning-text-button" :disabled="restoringTenderSource" @click="tenderSourceInput?.click()">
            <Upload :size="14"/>{{restoringTenderSource?'正在上传…':'补传原始 DOCX'}}
          </button>
        </div>
        <p v-if="!tablePreview.layout.supported && tablePreview.layout.tables.length" class="prompt-help">从旧项目保存的文字识别出 {{tablePreview.layout.tables.length}} 组表格。此预览无法恢复合并单元格和原始排版。</p>
        <p v-if="!tablePreview.layout.tables.length" class="prompt-help">没有可预览的表格。</p>
        <div v-if="tablePreview.layout.tables.length" class="tender-preview-tables">
          <section v-for="(table,index) in tablePreview.layout.tables" :key="index" class="tender-preview-table">
            <h4>表格 {{index+1}}</h4>
            <TenderTable :table="table" fill-gaps/>
          </section>
        </div>
      </template>
      <details v-if="tablePreview?.showSplitText" class="split-input-details" open>
        <summary>LangGraph 拆分输入 · {{selected?.tender_compact_text?.length||0}} 字</summary>
        <div v-if="tablePreview.layout?.split_blocks?.length" class="preview-text-content html-text-preview">
          <template v-for="(block,index) in tablePreview.layout.split_blocks" :key="index">
            <div v-if="block.kind==='table'" class="split-preview-inline-table">
              <TenderTable :table="block" fill-gaps/>
            </div>
            <component v-else :is="previewHeading.test(block.text.trim())?'h4':'p'">{{block.text}}</component>
          </template>
        </div>
        <div v-else-if="selected?.tender_compact_text" class="preview-text-content html-text-content" v-html="renderTextHtml(selected.tender_compact_text)"></div>
        <p v-else class="prompt-help">没有保存的拆分文本。</p>
      </details>
    </van-popup>
    <van-popup :show="!!planningTextPreview" @update:show="v=>!v&&(planningTextPreview=null)" position="center" round class="prompt-sheet text-preview-modal"><div class="sheet-title"><div><div class="eyebrow">PLANNING INPUT</div><h3>{{planningTextPreview?.title}}</h3></div><button class="icon-button" aria-label="关闭" @click="planningTextPreview=null">×</button></div><p class="prompt-help">{{planningTextPreview?.description}}</p><div class="preview-text-size"><span>文本大小</span><b>{{planningTextPreview?.content?.length||0}} 字</b><i>·</i><b>{{formatTextBytes(planningTextPreview?.content)}}</b></div><div class="preview-text-content html-text-content" v-html="renderTextHtml(planningTextPreview?.content)"></div></van-popup>
    <van-popup v-model:show="showHistory" position="right" class="drawer history-drawer"><div class="drawer-head"><button class="icon-button" @click="showHistory=false"><ArrowLeft :size="18"/></button><b>历史版本</b><span class="history-total">{{versions.length}} 条记录</span></div><div class="history-list"><div v-for="v in versions" :key="v.id" class="version-item"><div class="version-head"><span class="version-tag">V{{v.version_no}}</span><b>{{v.chapter_title}}</b></div><small>{{formatVersionDate(v.created_at)}}</small><pre>{{v.content}}</pre></div><div v-if="!versions.length" class="empty-history">暂无历史版本</div></div></van-popup>
  </main>
</template>
