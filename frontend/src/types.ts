export interface SchemaField {
  name: string
  label: string
  type: string
  description?: string
  aliases?: string[]
  role?: string
  aggregation?: string
  source_type?: string
  data_profile?: Record<string, unknown>
  index_content?: Record<string, string>
}

export interface SchemaTable {
  id: string
  name?: string
  label: string
  description: string
  fields: SchemaField[]
  database?: string
  domain?: string
  business_terms?: string[]
  primary_key?: string[]
  row_count?: number
}

export interface ClarificationOption {
  id: string
  label: string
  description: string
  recommended: boolean
}

export interface SchemaHit {
  doc_id: string
  table_id: string
  table_label: string
  field_name: string
  field_label: string
  score: number
  source?: "retrieval" | "user_confirmed" | "relation_key"
  bm25_rank?: number | null
  dense_rank?: number | null
  rerank_score?: number
}

export interface SchemaGraph {
  database: string
  graph_version: string
  tables: { id: string; name?: string; label: string; description: string; domain: string; database: string }[]
  fields: {
    id: string
    table_id: string
    table_name?: string
    sql_name?: string
    name: string
    label: string
    type: string
    description: string
    role: string
    source: string
    score: number
  }[]
  joins: {
    left_table: string
    left_field: string
    right_table: string
    right_field: string
    description: string
    relation_type: string
  }[]
}

export interface DatabaseHandoff {
  database: { name: string; dialect: string; route: string }
  required_context: {
    schema_graph: SchemaGraph
    resolved_parameters: Record<string, unknown>
    tool_facts: { tool: string; arguments: Record<string, unknown>; result: Record<string, unknown> }[]
  }
  instruction: {
    task: string
    filters: string[]
    joins: string[]
    group_by: string[]
    metrics: string[]
    order_by: string[]
  }
  output_contract: {
    row_grain: string
    columns: { name: string; alias: string; type: string }[]
    max_rows: number
    empty_result_policy: string
  }
}

export interface CoderToolCall {
  call_index: number
  database: string
  arguments: DatabaseHandoff | {
    mode: "single_database_agent"
    transport: "mcp_in_process"
    tool_name?: string
    schema_graph_version?: string
    sql_source?: string
  }
  sql: string
  success: boolean
  row_count: number
  error?: string | null
}

export interface VisualizationSpec {
  type: "bar" | "pie"
  title: string
  source_task_id: string
  category_field: string
  value_field: string
  category_labels?: Record<string, string>
  value_label?: string
  max_items: number
}

export interface AnalysisReport {
  title: string
  markdown: string
  summary?: string
  visualizations: VisualizationSpec[]
}

export interface ReportToolCall {
  call_index: number
  tool: "build_bar_chart" | "build_pie_chart"
  arguments: Record<string, unknown>
  result: VisualizationSpec
}

export interface QueryResult {
  task_id: string
  status: "waiting_clarification" | "completed" | "partial" | "failed"
  route: "database_query" | "data_qa" | "direct_response"
  message: string
  interpretation: null | {
    metric: string
    dimension: string
    time_range: string
    table: string
    assumptions: string[]
  }
  clarification: null | {
    parameter?: string
    question: string
    reason: string
    options: ClarificationOption[]
    allow_free_text?: boolean
  }
  steps: string[]
  sql: string | null
  columns: string[]
  rows: Record<string, string | number | null>[]
  truncated?: boolean
  warnings?: string[]
  analysis: string | null
  saved: boolean
  result_title?: string | null
  standalone_query?: string | null
  request_understanding?: {
    summary: string
    standard_request: string
    status: "ready" | "needs_clarification"
    clarifications: { question: string; answer: string; direction?: string; option_id?: string }[]
  } | null
  schema_graph?: SchemaGraph | null
  retrieval?: null | {
    threshold: number
    bm25_count: number
    dense_count: number
    rrf_count: number
    candidate_count: number
    selected_count: number
    embedding_source: string
    rerank_source: string
    hits: SchemaHit[]
    schema_graph?: SchemaGraph
  }
  tool_calls?: CoderToolCall[]
  analysis_sources?: AnalysisSource[]
  workflow_mode?: "qa" | "single_database_fast_path" | "multi_database_handoff" | "langgraph_hitl" | string | null
  report?: AnalysisReport | null
  report_tool_calls?: ReportToolCall[]
  analysis_plan?: AnalysisStep[]
  result_artifacts?: ResultArtifact[]
  analysis_claims?: { text: string; evidence_ids: string[] }[]
  analysis_hypotheses?: { statement: string; status: "unverified"; evidence_ids: string[] }[]
  analysis_limitations?: string[]
  stop_reason?: string | null
  analysis_budget?: Record<string, number>
}

export type QueryMode = "auto" | "query" | "analysis"
export interface AnalysisStep { id: string; title: string; status: "pending" | "completed"; depends_on: string[]; evidence_ids: string[] }
export interface ResultArtifact {
  result_id: string
  title: string
  columns: string[]
  rows: Record<string, string | number | null>[]
  time_range: string
  truncated: boolean
  limited: boolean
  semantic_verified: boolean
  notes?: string[]
  sql?: string
  derived_from: string[]
}

export interface DurableTask {
  task_id: string
  session_id: string
  query: string
  workspace: WorkspaceConfig
  state: "queued" | "running" | "waiting_clarification" | "completed" | "partial" | "failed" | "cancel_requested" | "cancelled" | "timed_out"
  stage: string
  version: number
  created_at: number
  updated_at: number
  elapsed_seconds: number
  recovery_count: number
  result: QueryResult | null
  analysis_progress?: { plan: AnalysisStep[]; result_count: number } | null
}

export interface ReportDataSource {
  taskId: string
  title: string
  columns: string[]
  rows: Record<string, string | number | null>[]
}

export interface WorkspaceConfig {
  schema_fields?: ConfirmedField[]
  analysis_table_ids?: string[]
  analysis_tables?: AnalysisTablePayload[]
  fields?: ConfirmedField[]
}

export interface AnalysisTablePayload {
  task_id: string
  title: string
  query: string
  columns: string[]
  rows: Record<string, string | number | null>[]
}

export interface AnalysisSource {
  task_id: string
  title: string
  query: string
  row_count: number
  columns: string[]
}

export interface ConfirmedField {
  name: string
  aggregation?: "auto"
  tableId?: string
}

export interface SavedMemory {
  id: string
  kind: "result_table" | "schema_field"
  created_at: string
  task_id?: string
  query?: string
  summary?: string
  title?: string
  columns?: string[]
  rows?: Record<string, string | number | null>[]
  table_id?: string
  name?: string
  label?: string
  field_type?: string
  user_id?: string
}

export interface AuthUser {
  user_id: string
  username: string
  display_name: string
  role: "admin" | "current_sales" | string
}

export interface LoginResponse {
  access_token: string
  token_type: "bearer"
  user: AuthUser
}
