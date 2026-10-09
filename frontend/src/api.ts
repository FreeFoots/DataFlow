import type { AuthUser, DurableTask, LoginResponse, QueryMode, QueryResult, SavedMemory, SchemaField, SchemaTable, WorkspaceConfig } from "./types"

const API_BASE = import.meta.env.VITE_API_BASE ?? ""
const TOKEN_KEY = "dataflow_next_access_token"
const LEGACY_TOKEN_KEY = "dataflow_next_legacy_access_token"

export class ApiError extends Error {
  constructor(message: string, public status: number) { super(message) }
}

function token() {
  const current = sessionStorage.getItem(TOKEN_KEY)
  if (current) return current
  const legacy = sessionStorage.getItem(LEGACY_TOKEN_KEY)
  if (legacy) {
    sessionStorage.setItem(TOKEN_KEY, legacy)
    sessionStorage.removeItem(LEGACY_TOKEN_KEY)
  }
  return legacy
}

async function request<T>(path: string, options?: RequestInit, timeoutMs = 90_000): Promise<T> {
  const controller = new AbortController()
  const timeout = window.setTimeout(() => controller.abort(), timeoutMs)
  let response: Response
  try {
    response = await fetch(`${API_BASE}${path}`, {
      ...options,
      signal: controller.signal,
      headers: {
        "Content-Type": "application/json",
        ...(token() ? { Authorization: `Bearer ${token()}` } : {}),
        ...options?.headers,
      },
    })
  } catch (error) {
    if (error instanceof DOMException && error.name === "AbortError") {
      throw new Error("查询等待时间较长，请稍后重试；原请求可能仍在处理")
    }
    throw new Error("暂时无法连接服务，请稍后重试或联系管理员")
  } finally {
    window.clearTimeout(timeout)
  }
  if (!response.ok) {
    const body = await response.json().catch(() => ({}))
    throw new ApiError(body.detail ?? "服务暂时不可用", response.status)
  }
  return response.json() as Promise<T>
}

export const api = {
  hasSession: () => Boolean(token()),
  login: async (username: string, password: string) => {
    const result = await request<LoginResponse>("/api/auth/login", {
      method: "POST",
      body: JSON.stringify({ username, password }),
    })
    sessionStorage.removeItem(LEGACY_TOKEN_KEY)
    sessionStorage.setItem(TOKEN_KEY, result.access_token)
    return result.user
  },
  me: () => request<AuthUser>("/api/auth/me"),
  logout: async () => {
    try {
      if (token()) await request<{ logged_out: boolean }>("/api/auth/logout", { method: "POST" })
    } finally {
      sessionStorage.removeItem(TOKEN_KEY)
      sessionStorage.removeItem(LEGACY_TOKEN_KEY)
    }
  },
  clearSession: () => {
    sessionStorage.removeItem(TOKEN_KEY)
    sessionStorage.removeItem(LEGACY_TOKEN_KEY)
  },
  schema: () => request<SchemaTable[]>("/api/schema"),
  tasks: () => request<DurableTask[]>("/api/task-history"),
  task: (id: string) => request<DurableTask>(`/api/tasks/${encodeURIComponent(id)}/state`, undefined, 15_000),
  submitTask: (query: string, workspace: WorkspaceConfig, sessionId: string, submissionKey: string, mode: QueryMode = "auto") =>
    request<DurableTask>("/api/tasks", { method: "POST", body: JSON.stringify({ query, workspace, session_id: sessionId, submission_key: submissionKey, mode }) }, 15_000),
  resumeTask: (id: string, optionId: string, version: number, key: string, answer?: string) =>
    request<DurableTask>(`/api/tasks/${encodeURIComponent(id)}/resume`, { method: "POST", body: JSON.stringify({ option_id: optionId, answer, version, submission_key: key }) }, 15_000),
  cancelTask: (id: string) => request<DurableTask>(`/api/tasks/${encodeURIComponent(id)}/cancel`, { method: "POST" }),
  hideSession: (id: string) => request<{ hidden: boolean }>(`/api/sessions/${encodeURIComponent(id)}/hide`, { method: "POST" }),
  query: (query: string, workspace: WorkspaceConfig, sessionId: string) =>
    request<QueryResult>("/api/query", {
      method: "POST",
      body: JSON.stringify({ query, session_id: sessionId, workspace }),
    }, 300_000),
  clarify: (taskId: string, optionId: string) =>
    request<QueryResult>(`/api/tasks/${taskId}/clarify`, {
      method: "POST",
      body: JSON.stringify({ option_id: optionId }),
    }, 300_000),
  save: (taskId: string) =>
    request<{ saved: boolean }>("/api/memories", {
      method: "POST",
      body: JSON.stringify({ task_id: taskId }),
    }),
  memories: () => request<SavedMemory[]>("/api/memories"),
  saveField: (tableId: string, field: SchemaField) =>
    request<{ saved: boolean }>("/api/memories/fields", {
      method: "POST",
      body: JSON.stringify({
        table_id: tableId,
        name: field.name,
        label: field.label,
        field_type: field.type,
      }),
    }),
  deleteMemory: (memoryId: string) =>
    request<{ deleted: boolean }>(`/api/memories/${encodeURIComponent(memoryId)}`, {
      method: "DELETE",
    }),
}
