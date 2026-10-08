<script setup lang="ts">
import { computed, nextTick, onMounted, ref } from "vue"
import { api } from "./api"
import ReportCard from "./components/ReportCard.vue"
import ResultTableCard from "./components/ResultTableCard.vue"
import type { AuthUser, QueryResult, ReportDataSource, SavedMemory, SchemaField, SchemaTable, WorkspaceConfig } from "./types"

interface ConversationTurn {
  query: string
  result: QueryResult
}

interface ConversationRecord {
  id: string
  title: string
  updatedAt: number
  turns: ConversationTurn[]
  workspace: WorkspaceConfig
}

interface HistoricalResultTable {
  taskId: string
  title: string
  query: string
  rowCount: number
  columns: string[]
  rows: Record<string, string | number | null>[]
}

const SAVED_TABLE_LIMIT = 8
const FIELD_LIBRARY_LIMIT = 12
const input = ref("")
const loading = ref(false)
const pendingQuery = ref("")
const error = ref("")
const schema = ref<SchemaTable[]>([])
const savedMemories = ref<SavedMemory[]>([])
const workspace = ref<WorkspaceConfig>({})
const conversations = ref<ConversationRecord[]>([])
const activeConversationId = ref("")
const expandedSql = ref<string | null>(null)
const leftOpen = ref(false)
const rightOpen = ref(false)
const conversationScroll = ref<HTMLElement | null>(null)
const authUser = ref<AuthUser | null>(null)
const authReady = ref(false)
const loginLoading = ref(false)
const loginUsername = ref("admin")
const loginPassword = ref("admin123")
const prompts = computed(() => {
  const role = authUser.value?.role
  if (role === "growth_ops") return ["查询2026年每个月的新增用户数", "查询2026年4月各渠道的注册用户数", "查询自然推荐渠道激活用户的平均激活耗时"]
  if (role === "channel_ops") return ["查询2026年4月每天的广告消耗", "查询2026年4月各渠道的广告消耗", "查询2026年4月点击量最高的5个广告计划"]
  if (role === "content_ops") return ["查询2026年4月每天的视频曝光量和播放量", "查询知识类内容的平均观看时长和平均完播率", "查询各内容分类的内容数量"]
  return ["查询2026年每个月的新增用户数", "查询2026年4月每天的广告消耗", "查询知识类内容的平均观看时长和平均完播率"]
})

const activeConversation = computed(() =>
  conversations.value.find((item) => item.id === activeConversationId.value),
)
const confirmedFields = computed(() => workspace.value.schema_fields ?? [])
const analysisTableIds = computed(() => workspace.value.analysis_table_ids ?? [])
const historicalResultTables = computed<HistoricalResultTable[]>(() => {
  return savedMemories.value
    .filter((item) => item.kind === "result_table" && item.task_id && item.rows?.length)
    .map((item) => ({
      taskId: item.task_id!,
      title: item.title || "查询结果",
      query: item.query || "",
      rowCount: item.rows?.length || 0,
      columns: item.columns || [],
      rows: item.rows || [],
    }))
    .slice(0, SAVED_TABLE_LIMIT)
})
const reportDataSources = computed<ReportDataSource[]>(() => {
  const sources = new Map<string, ReportDataSource>()
  for (const conversation of conversations.value) {
    for (const turn of conversation.turns) {
      if (!turn.result.rows.length) continue
      sources.set(turn.result.task_id, {
        taskId: turn.result.task_id,
        title: turn.result.result_title || "查询结果",
        columns: turn.result.columns,
        rows: turn.result.rows,
      })
    }
  }
  for (const table of historicalResultTables.value) {
    sources.set(table.taskId, {
      taskId: table.taskId,
      title: table.title,
      columns: table.columns,
      rows: table.rows,
    })
  }
  return [...sources.values()]
})
const selectedAnalysisTables = computed(() =>
  analysisTableIds.value
    .map((id) => historicalResultTables.value.find((table) => table.taskId === id))
    .filter((table): table is HistoricalResultTable => Boolean(table)),
)
const recentFields = computed(() => {
  const items: { field: SchemaField; tableId: string }[] = []
  const keys = new Set<string>()
  const add = (fieldName: string, tableId: string) => {
    const table = schema.value.find((item) => item.id === tableId)
    const field = table?.fields.find((item) => item.name === fieldName)
    const key = `${tableId}:${fieldName}`
    if (field && !keys.has(key)) {
      keys.add(key)
      items.push({ field, tableId })
    }
  }
  for (const conversation of conversations.value) {
    for (const turn of [...conversation.turns].reverse()) {
      for (const field of turn.result.schema_graph?.fields ?? []) {
        if (field.source !== "relation_key") add(field.name, field.table_id)
      }
    }
    for (const field of conversation.workspace.schema_fields ?? conversation.workspace.fields ?? []) {
      const tableId = field.tableId
      if (tableId) add(field.name, tableId)
    }
  }
  return items.slice(0, FIELD_LIBRARY_LIMIT)
})
const savedFieldKeys = computed(() => new Set(
  savedMemories.value
    .filter((item) => item.kind === "schema_field")
    .map((item) => `${item.table_id}:${item.name}`),
))
const fieldLibrary = computed(() => {
  const items: { field: SchemaField; tableId: string; saved: boolean }[] = []
  const keys = new Set<string>()
  const add = (field: SchemaField, tableId: string, saved: boolean) => {
    const key = `${tableId}:${field.name}`
    if (keys.has(key)) return
    keys.add(key)
    items.push({ field, tableId, saved })
  }
  for (const memory of savedMemories.value) {
    if (memory.kind !== "schema_field" || !memory.table_id || !memory.name) continue
    const field = findField(memory.name, memory.table_id) ?? {
      name: memory.name,
      label: memory.label || memory.name,
      type: memory.field_type || "文本",
    }
    add(field, memory.table_id, true)
  }
  for (const item of recentFields.value) {
    add(item.field, item.tableId, savedFieldKeys.value.has(`${item.tableId}:${item.field.name}`))
  }
  return items.slice(0, FIELD_LIBRARY_LIMIT)
})
const historyGroups = computed(() => {
  const today = new Date().toDateString()
  const recent: ConversationRecord[] = []
  const earlier: ConversationRecord[] = []
  for (const item of conversations.value) {
    if (new Date(item.updatedAt).toDateString() === today) recent.push(item)
    else earlier.push(item)
  }
  return [
    { label: "今天", items: recent },
    { label: "更早", items: earlier },
  ].filter((group) => group.items.length)
})
const userRoleLabel = computed(() => {
  if (authUser.value?.role === "admin") return "全部数据权限"
  if (authUser.value?.role === "growth_ops") return "用户增长数据"
  if (authUser.value?.role === "channel_ops") return "渠道投放数据"
  if (authUser.value?.role === "content_ops") return "内容运营数据"
  return "数据权限待确认"
})

onMounted(async () => {
  if (api.hasSession()) {
    try {
      authUser.value = await api.me()
    } catch {
      api.clearSession()
    }
  }
  authReady.value = true
  if (authUser.value) await initializeUserWorkspace()
})

async function initializeUserWorkspace() {
  conversations.value = []
  activeConversationId.value = ""
  workspace.value = {}
  schema.value = []
  savedMemories.value = []
  createConversation()
  try {
    const [schemaResult, memories] = await Promise.all([api.schema(), api.memories()])
    schema.value = schemaResult
    savedMemories.value = memories
  } catch (caught) {
    error.value = caught instanceof Error ? caught.message : "工作区加载失败"
  }
}

function useMockAccount(username: string, password: string) {
  loginUsername.value = username
  loginPassword.value = password
  error.value = ""
}

async function loginUser() {
  if (!loginUsername.value.trim() || !loginPassword.value || loginLoading.value) return
  loginLoading.value = true
  error.value = ""
  try {
    authUser.value = await api.login(loginUsername.value.trim(), loginPassword.value)
    await initializeUserWorkspace()
  } catch (caught) {
    error.value = caught instanceof Error ? caught.message : "登录失败"
  } finally {
    loginLoading.value = false
  }
}

async function logoutUser() {
  if (loading.value) return
  try {
    await api.logout()
  } finally {
    authUser.value = null
    conversations.value = []
    activeConversationId.value = ""
    workspace.value = {}
    schema.value = []
    savedMemories.value = []
    input.value = ""
    error.value = ""
  }
}

function normalizeWorkspace(value?: WorkspaceConfig): WorkspaceConfig {
  const fields = value?.schema_fields ?? value?.fields ?? []
  return {
    schema_fields: Array.isArray(fields)
      ? fields.map((field) => ({ name: field.name, tableId: field.tableId, aggregation: "auto" as const }))
      : [],
    analysis_table_ids: Array.isArray(value?.analysis_table_ids)
      ? [...value.analysis_table_ids]
      : [],
  }
}

function tidyConversations() {
  conversations.value.sort((a, b) => b.updatedAt - a.updatedAt)
  conversations.value = conversations.value.slice(0, 30)
}

function createConversation() {
  if (loading.value) return
  const existingEmpty = conversations.value.find((item) => !item.turns.length)
  if (existingEmpty) {
    selectConversation(existingEmpty.id)
    return
  }
  const item: ConversationRecord = {
    id: crypto.randomUUID(),
    title: "新对话",
    updatedAt: Date.now(),
    turns: [],
    workspace: {},
  }
  conversations.value.unshift(item)
  activeConversationId.value = item.id
  workspace.value = {}
  input.value = ""
  error.value = ""
  leftOpen.value = false
  tidyConversations()
}

function selectConversation(id: string) {
  const item = conversations.value.find((conversation) => conversation.id === id)
  if (!item) return
  activeConversationId.value = id
  workspace.value = normalizeWorkspace(item.workspace)
  input.value = ""
  error.value = ""
  leftOpen.value = false
  nextTick(() => conversationScroll.value?.scrollTo({ top: conversationScroll.value.scrollHeight }))
}

function removeConversation(id: string) {
  conversations.value = conversations.value.filter((item) => item.id !== id)
  if (activeConversationId.value === id) {
    if (conversations.value[0]) selectConversation(conversations.value[0].id)
    else createConversation()
  }
  tidyConversations()
}

function saveActiveConversation() {
  const conversation = activeConversation.value
  if (!conversation) return
  conversation.workspace = normalizeWorkspace(workspace.value)
  conversation.updatedAt = Date.now()
  if (conversation.title === "新对话" && conversation.turns[0]) {
    conversation.title = conversation.turns[0].query.slice(0, 18)
  }
  tidyConversations()
}

async function submit(text?: string) {
  const query = (text ?? input.value).trim()
  if (!query || loading.value) return
  if (!activeConversation.value) createConversation()
  loading.value = true
  pendingQuery.value = query
  error.value = ""
  input.value = ""
  try {
    const requestWorkspace: WorkspaceConfig = {
      ...workspace.value,
      analysis_tables: selectedAnalysisTables.value.map((table) => ({
        task_id: table.taskId,
        title: table.title,
        query: table.query,
        columns: table.columns,
        rows: table.rows.slice(0, 50),
      })),
    }
    const result = await api.query(
      query,
      requestWorkspace,
      activeConversation.value?.id ?? "studio-demo",
    )
    activeConversation.value?.turns.push({ query, result })
    saveActiveConversation()
  } catch (caught) {
    error.value = caught instanceof Error ? caught.message : "查询失败"
    input.value = query
  } finally {
    loading.value = false
    pendingQuery.value = ""
  }
}

async function saveResult(result: QueryResult) {
  await api.save(result.task_id)
  result.saved = true
  savedMemories.value = await api.memories()
  saveActiveConversation()
}

async function toggleSavedField(field: SchemaField, tableId: string) {
  const memoryId = `field:${tableId}.${field.name}`
  if (savedFieldKeys.value.has(`${tableId}:${field.name}`)) {
    await api.deleteMemory(memoryId)
  } else {
    await api.saveField(tableId, field)
  }
  savedMemories.value = await api.memories()
}

function isSavedField(fieldName: string, tableId?: string) {
  return Boolean(tableId && savedFieldKeys.value.has(`${tableId}:${fieldName}`))
}

function toggleConfirmedFieldMemory(fieldName: string, tableId?: string) {
  const field = findField(fieldName, tableId)
  if (field && tableId) toggleSavedField(field, tableId)
}

function resetWorkspace() {
  workspace.value = {}
  saveActiveConversation()
}

function dragAnalysisTable(event: DragEvent, table: HistoricalResultTable) {
  event.dataTransfer?.setData("analysis-result-table", table.taskId)
  if (event.dataTransfer) event.dataTransfer.effectAllowed = "copy"
}

function addAnalysisTable(taskId: string) {
  if (!historicalResultTables.value.some((table) => table.taskId === taskId) || analysisTableIds.value.includes(taskId)) return
  workspace.value.analysis_table_ids = [...analysisTableIds.value, taskId]
  saveActiveConversation()
}

function dropAnalysisTable(event: DragEvent) {
  addAnalysisTable(event.dataTransfer?.getData("analysis-result-table") ?? "")
}

function removeAnalysisTable(taskId: string) {
  workspace.value.analysis_table_ids = analysisTableIds.value.filter((id) => id !== taskId)
  saveActiveConversation()
}

function dragField(event: DragEvent, field: SchemaField, tableId: string) {
  event.dataTransfer?.setData("field", field.name)
  event.dataTransfer?.setData("field-table", tableId)
  if (event.dataTransfer) event.dataTransfer.effectAllowed = "copy"
}

function findField(fieldName: string, tableId?: string) {
  const selectedTable = tableId ? schema.value.find((table) => table.id === tableId) : undefined
  return selectedTable?.fields.find((field) => field.name === fieldName)
    ?? schema.value.flatMap((table) => table.fields).find((field) => field.name === fieldName)
}

function isNumericField(fieldName: string, tableId?: string) {
  const field = findField(fieldName, tableId)
  return field?.type === "数值" || field?.type === "整数"
}

function addConfirmedField(fieldName: string, sourceTableId?: string) {
  const tableId = sourceTableId
  if (!tableId || !findField(fieldName, tableId)) return
  if (confirmedFields.value.some((field) => field.name === fieldName && field.tableId === tableId)) return
  workspace.value.schema_fields = [
    ...confirmedFields.value,
    {
      name: fieldName,
      aggregation: "auto",
      tableId,
    },
  ]
  saveActiveConversation()
}

function dropConfirmedField(event: DragEvent) {
  addConfirmedField(
    event.dataTransfer?.getData("field") ?? "",
    event.dataTransfer?.getData("field-table") ?? undefined,
  )
}

function removeConfirmedField(fieldName: string, tableId?: string) {
  workspace.value.schema_fields = confirmedFields.value.filter(
    (field) => !(field.name === fieldName && field.tableId === tableId),
  )
  saveActiveConversation()
}

function fieldLabel(fieldName: string, tableId?: string) {
  return findField(fieldName, tableId)?.label ?? fieldName
}

function tableLabel(tableId?: string) {
  return schema.value.find((table) => table.id === tableId)?.label ?? "未知数据表"
}

function clarificationHint(result: QueryResult) {
  return result.clarification?.options.map((option) => option.label).join("、") ?? ""
}

</script>

<template>
  <div v-if="!authReady" class="auth-loading"><span></span><p>正在连接 DataFlow…</p></div>

  <main v-else-if="!authUser" class="login-page">
    <section class="login-intro">
      <div class="login-brand"><span>D</span><strong>DataFlow</strong></div>
      <div>
        <p class="kicker">AI DATA ASSISTANT</p>
        <h1>用自然语言，<br>读懂你的数据。</h1>
        <p>字段级 Schema 检索、权限隔离和可保存的数据记忆，都从一个问题开始。</p>
      </div>
      <small>FastAPI · LangGraph · MCP</small>
    </section>

    <section class="login-side">
      <form class="login-card" @submit.prevent="loginUser">
        <header><span class="login-mark">D</span><div><h2>欢迎回来</h2><p>登录 DataFlow</p></div></header>
        <label>
          <span>账号</span>
          <input v-model="loginUsername" autocomplete="username" placeholder="请输入账号">
        </label>
        <label>
          <span>密码</span>
          <input v-model="loginPassword" type="password" autocomplete="current-password" placeholder="请输入密码">
        </label>
        <p v-if="error" class="login-error">{{ error }}</p>
        <button class="login-submit" :disabled="loginLoading" type="submit">
          {{ loginLoading ? "正在登录…" : "登录" }}
        </button>

        <div class="mock-accounts">
          <p>演示账号</p>
          <button type="button" @click="useMockAccount('admin', 'admin123')">
            <span class="account-icon admin">管</span>
            <span><strong>admin</strong><small>管理员 · 全部数据</small></span>
            <code>admin123</code>
          </button>
          <button type="button" @click="useMockAccount('growth', 'growth123')">
            <span class="account-icon user">增</span>
            <span><strong>growth</strong><small>用户增长 · 18张表</small></span>
            <code>growth123</code>
          </button>
          <button type="button" @click="useMockAccount('channel', 'channel123')">
            <span class="account-icon mock">渠</span>
            <span><strong>channel</strong><small>渠道投放 · 16张表</small></span>
            <code>channel123</code>
          </button>
          <button type="button" @click="useMockAccount('content', 'content123')">
            <span class="account-icon mock">内</span>
            <span><strong>content</strong><small>内容运营 · 19张表</small></span>
            <code>content123</code>
          </button>
        </div>
        <small class="login-note">Mock 登录仅用于本地学习，不适合生产环境。</small>
      </form>
    </section>
  </main>

  <div v-else class="app-layout">
    <aside class="history-sidebar" :class="{ open: leftOpen }">
      <div class="brand-row">
        <span class="brand-symbol">D</span>
        <div><strong>DataFlow</strong><small>AI 数据分析</small></div>
      </div>

      <button class="new-chat" :disabled="loading || !activeConversation?.turns.length" @click="createConversation"><span>＋</span>新建对话</button>

      <nav class="history-list" aria-label="对话历史">
        <section v-for="group in historyGroups" :key="group.label">
          <p>{{ group.label }}</p>
          <div
            v-for="item in group.items"
            :key="item.id"
            class="history-item"
            :class="{ active: item.id === activeConversationId }"
          >
            <button class="history-select" @click="selectConversation(item.id)">
              <span class="chat-icon">◌</span>
              <span><strong>{{ item.title }}</strong><small>{{ item.turns.length }} 轮对话</small></span>
            </button>
            <button class="history-remove" aria-label="删除对话" @click="removeConversation(item.id)">×</button>
          </div>
        </section>
      </nav>

      <div class="history-footer">
        <div class="user-avatar">{{ authUser.display_name.slice(0, 1) }}</div>
        <div class="history-user"><strong>{{ authUser.display_name }}</strong><small><i></i> {{ userRoleLabel }}</small></div>
        <button class="logout-button" @click="logoutUser">退出</button>
      </div>
    </aside>

    <main class="chat-column">
      <header class="chat-header">
        <button class="mobile-menu" @click="leftOpen = !leftOpen">☰</button>
        <div>
          <strong>{{ activeConversation?.title ?? "新对话" }}</strong>
          <span>自然语言问数</span>
        </div>
        <button class="config-trigger" @click="rightOpen = !rightOpen">上下文设置</button>
        <span class="model-state"><i></i> 运营问数</span>
      </header>

      <section ref="conversationScroll" class="conversation-scroll">
        <Transition name="conversation" mode="out-in">
        <div :key="activeConversationId" class="conversation-view">
        <div v-if="!activeConversation?.turns.length && !loading" class="welcome-panel">
          <div class="welcome-copy">
            <span class="welcome-mark">✦</span>
            <p class="kicker">DATAFLOW</p>
            <h1>想从数据里了解什么？</h1>
            <p>查询结果会以清晰的表格卡片展示，并支持分页和Excel导出。</p>
            <div class="prompt-list">
              <button v-for="prompt in prompts" :key="prompt" @click="submit(prompt)">
                <span>↗</span>{{ prompt }}
              </button>
            </div>
          </div>
        </div>

        <div v-else class="thread">
          <article v-for="(turn, turnIndex) in activeConversation?.turns" :key="`${turn.result.task_id}:${turnIndex}`" class="turn">
            <div class="user-message">
              <div class="message-avatar user">你</div>
              <div class="message-body"><small>你</small><p>{{ turn.query }}</p></div>
            </div>

            <div class="assistant-message">
              <div class="message-avatar assistant">D</div>
              <div class="message-body assistant-body">
                <div class="answer-heading" :class="{ 'qa-heading': turn.result.route !== 'database_query' }">
                  <div><small>DataFlow</small><strong v-if="turn.result.route === 'database_query'">{{ turn.result.status === "failed" ? "处理失败" : turn.result.status === "waiting_clarification" ? "需要补充信息" : "查询完成" }}</strong></div>
                  <span v-if="turn.result.route === 'database_query' && turn.result.status === 'completed'">
                    {{ turn.result.workflow_mode === "single_database_agent" ? "单库智能体" : "多库流程" }}
                  </span>
                </div>

                <div v-if="turn.result.status === 'waiting_clarification'" class="natural-clarification">
                  <p>{{ turn.result.clarification?.question }}</p>
                  <small>{{ turn.result.clarification?.reason }}</small>
                  <span v-if="clarificationHint(turn.result)">请直接回复：{{ clarificationHint(turn.result) }}</span>
                </div>

                <details v-if="turn.result.retrieval && turn.result.status !== 'waiting_clarification'" class="execution-details">
                  <summary>查看检索过程</summary>
                  <div class="execution-strip">
                    <span>BM25 {{ turn.result.retrieval.bm25_count }} · Dense {{ turn.result.retrieval.dense_count }}</span>
                    <span>RRF {{ turn.result.retrieval.rrf_count }} → Rerank {{ turn.result.retrieval.selected_count }} 字段</span>
                    <span>阈值 ≥ {{ turn.result.retrieval.threshold.toFixed(2) }}</span>
                    <span v-if="turn.result.schema_graph">Schema 图 {{ turn.result.schema_graph.tables.length }} 表 · {{ turn.result.schema_graph.fields.length }} 字段</span>
                  </div>
                </details>

                <div
                  v-if="turn.result.standalone_query && turn.result.standalone_query !== turn.query"
                  class="standalone-query"
                >
                  <small>本轮独立查询</small>
                  <span>{{ turn.result.standalone_query }}</span>
                </div>

                <div v-if="turn.result.analysis_sources?.length" class="analysis-source-strip">
                  <strong>综合分析来源</strong>
                  <span v-for="source in turn.result.analysis_sources" :key="source.task_id">
                    ▦ {{ source.title }} · {{ source.row_count }} 行
                  </span>
                </div>

                <div v-if="turn.result.interpretation" class="scope-card">
                  <div class="scope-head"><strong>本次查询口径</strong><span>模型识别口径</span></div>
                  <div class="scope-grid">
                    <div><small>数据表</small><strong>{{ turn.result.interpretation.table }}</strong></div>
                    <div><small>指标</small><strong>{{ turn.result.interpretation.metric }}</strong></div>
                    <div><small>维度</small><strong>{{ turn.result.interpretation.dimension }}</strong></div>
                    <div><small>时间</small><strong>{{ turn.result.interpretation.time_range }}</strong></div>
                  </div>
                </div>

                <ReportCard
                  v-if="turn.result.report"
                  :report="turn.result.report"
                  :sources="reportDataSources"
                />

                <p v-else-if="turn.result.route !== 'database_query' && turn.result.analysis" class="qa-answer">{{ turn.result.analysis }}</p>

                <ResultTableCard
                  v-if="turn.result.route === 'database_query' && turn.result.status === 'completed' && turn.result.rows.length"
                  :title="turn.result.result_title || '查询结果'"
                  :columns="turn.result.columns"
                  :rows="turn.result.rows"
                  :truncated="turn.result.truncated"
                >
                  <template #actions>
                      <button v-if="turn.result.sql" @click="expandedSql = expandedSql === turn.result.task_id ? null : turn.result.task_id">SQL</button>
                      <button class="save-result" :disabled="turn.result.saved" @click="saveResult(turn.result)">{{ turn.result.saved ? "已保存" : "保存" }}</button>
                  </template>
                  <template #details>
                    <pre v-if="expandedSql === turn.result.task_id && turn.result.sql"><code>{{ turn.result.sql }}</code></pre>
                  </template>
                </ResultTableCard>

                <div
                  v-if="turn.result.route === 'database_query' && turn.result.analysis && turn.result.status !== 'waiting_clarification'"
                  class="result-analysis"
                >
                  <strong>结果说明</strong>
                  <p>{{ turn.result.analysis }}</p>
                </div>
              </div>
            </div>
          </article>

          <div v-if="loading && pendingQuery" class="user-message pending-user-message">
            <div class="message-avatar user">你</div>
            <div class="message-body"><small>你 · 已发送</small><p>{{ pendingQuery }}</p></div>
          </div>

          <div v-if="loading" class="assistant-message loading-message">
            <div class="message-avatar assistant">D</div>
            <div class="loading-copy"><span></span><div><strong>正在分析</strong><small>理解问题并检索相关 Schema…</small></div></div>
          </div>
        </div>
        </div>
        </Transition>

        <div v-if="error" class="error-banner">{{ error }}</div>
      </section>

      <footer class="composer">
        <div class="composer-box">
          <textarea v-model="input" rows="1" placeholder="向数据提问…" @keydown.enter.exact.prevent="submit()"></textarea>
          <div class="composer-meta">
            <span>问数 {{ confirmedFields.length }} 个字段 · 分析 {{ selectedAnalysisTables.length }} 张结果表</span>
            <button :disabled="!input.trim() || loading" @click="submit()">↑</button>
          </div>
        </div>
        <small>模型可能产生偏差，缺少关键业务口径时会在对话中继续询问。</small>
      </footer>
    </main>

    <aside class="query-panel" :class="{ open: rightOpen }">
      <div class="panel-header">
        <div><p class="kicker">CONTEXT CONTROL</p><h2>本次上下文</h2></div>
        <button @click="resetWorkspace">清空</button>
      </div>

      <section class="context-block query-context-block">
        <div class="context-heading">
          <div><strong>查询字段</strong><small>用于生成 SQL</small></div>
          <em>{{ confirmedFields.length }} 个</em>
        </div>
        <div
          class="confirmed-fields-zone"
          :class="{ filled: confirmedFields.length }"
          @dragover.prevent
          @drop.prevent="dropConfirmedField"
        >
          <div v-if="!confirmedFields.length" class="empty-confirmation">
            <span>＋</span>
            <strong>拖入字段</strong>
          </div>
          <div v-for="field in confirmedFields" :key="`${field.tableId}:${field.name}`" class="confirmed-field">
            <span class="field-badge">{{ isNumericField(field.name, field.tableId) ? "#" : "T" }}</span>
            <span class="confirmed-field-name"><strong>{{ fieldLabel(field.name, field.tableId) }}</strong><small>{{ tableLabel(field.tableId) }}</small></span>
            <button class="save-field" :class="{ saved: isSavedField(field.name, field.tableId) }" :aria-label="`保存${fieldLabel(field.name, field.tableId)}`" @click="toggleConfirmedFieldMemory(field.name, field.tableId)">{{ isSavedField(field.name, field.tableId) ? "★" : "☆" }}</button>
            <button class="remove-field" :aria-label="`移除${fieldLabel(field.name, field.tableId)}`" @click="removeConfirmedField(field.name, field.tableId)">×</button>
          </div>
        </div>
      </section>

      <section class="context-block analysis-context-block">
        <div class="context-heading">
          <div><strong>参考结果</strong><small>用于综合分析</small></div>
          <em>{{ selectedAnalysisTables.length }} 张</em>
        </div>
        <div
          class="analysis-tables-zone"
          :class="{ filled: selectedAnalysisTables.length }"
          @dragover.prevent
          @drop.prevent="dropAnalysisTable"
        >
          <div v-if="!selectedAnalysisTables.length" class="empty-confirmation">
            <span>＋</span>
            <strong>拖入已保存结果</strong>
          </div>
          <div v-for="table in selectedAnalysisTables" :key="table.taskId" class="analysis-table-chip">
            <span class="table-mark">▦</span>
            <span><strong>{{ table.title }}</strong><small>{{ table.rowCount }} 行 · {{ table.columns.slice(0, 3).join(" / ") }}</small></span>
            <button :aria-label="`移除${table.title}`" @click="removeAnalysisTable(table.taskId)">×</button>
          </div>
        </div>
      </section>

      <section class="recent-library">
        <div class="library-heading">
          <div><strong>字段与结果</strong><small>拖拽或双击添加</small></div>
        </div>
        <div class="recent-columns">
          <div class="recent-column">
            <p>字段 · 最多 {{ FIELD_LIBRARY_LIMIT }} 个</p>
            <div v-if="!fieldLibrary.length" class="library-empty">查询后可收藏关键字段</div>
            <div
              v-for="item in fieldLibrary"
              :key="`${item.tableId}:${item.field.name}`"
              class="field-library-row"
              draggable="true"
              @dragstart="dragField($event, item.field, item.tableId)"
            >
              <button class="recent-card field-card" @dblclick="addConfirmedField(item.field.name, item.tableId)">
                <span class="field-badge">{{ item.field.type === "数值" || item.field.type === "整数" ? "#" : "T" }}</span>
                <span><strong>{{ item.field.label }}</strong><small>{{ tableLabel(item.tableId) }}</small></span>
              </button>
              <button class="save-field" :class="{ saved: item.saved }" :aria-label="`${item.saved ? '取消收藏' : '收藏'}${item.field.label}`" @click="toggleSavedField(item.field, item.tableId)">{{ item.saved ? "★" : "☆" }}</button>
            </div>
          </div>
          <div class="recent-column">
            <p>结果表 · 最多 {{ SAVED_TABLE_LIMIT }} 张</p>
            <div v-if="!historicalResultTables.length" class="library-empty">保存查询结果后显示</div>
            <button
              v-for="table in historicalResultTables"
              :key="table.taskId"
              class="recent-card table-card"
              draggable="true"
              @dragstart="dragAnalysisTable($event, table)"
              @dblclick="addAnalysisTable(table.taskId)"
            >
              <span class="table-mark">▦</span>
              <span><strong>{{ table.title }}</strong><small>{{ table.rowCount }} 行 · {{ table.query }}</small></span>
              <em>⋮⋮</em>
            </button>
          </div>
        </div>
      </section>
    </aside>

  </div>
</template>
