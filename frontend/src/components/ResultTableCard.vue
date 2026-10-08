<script setup lang="ts">
import { computed, ref, watch } from "vue"

const props = withDefaults(defineProps<{
  title: string
  columns: string[]
  rows: Record<string, unknown>[]
  updatedAt?: string
  loading?: boolean
  error?: string
  initialPageSize?: number
  truncated?: boolean
}>(), {
  updatedAt: "刚刚更新",
  loading: false,
  error: "",
  initialPageSize: 10,
  truncated: false,
})

const page = ref(1)
const pageSize = ref(props.initialPageSize)
const exporting = ref(false)
const totalPages = computed(() => Math.max(1, Math.ceil(props.rows.length / pageSize.value)))
const pagedRows = computed(() => {
  const start = (page.value - 1) * pageSize.value
  return props.rows.slice(start, start + pageSize.value)
})

watch(() => props.rows, () => { page.value = 1 })
watch(pageSize, () => { page.value = 1 })

function previousPage() {
  page.value = Math.max(1, page.value - 1)
}

function nextPage() {
  page.value = Math.min(totalPages.value, page.value + 1)
}

function formatCell(value: unknown, column: string) {
  if (value === null || value === undefined || value === "") return "—"
  if (typeof value !== "number") return String(value)
  if (/金额|销售额|客单价|收入/.test(column)) {
    return new Intl.NumberFormat("zh-CN", { style: "currency", currency: "CNY", maximumFractionDigits: 0 }).format(value)
  }
  if (/率|占比/.test(column)) return `${(value * 100).toFixed(1)}%`
  return value.toLocaleString("zh-CN", { maximumFractionDigits: 2 })
}

function xmlEscape(value: unknown) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&apos;")
}

function excelCell(value: unknown, style = "Data") {
  const isNumber = typeof value === "number" && Number.isFinite(value)
  return `<Cell ss:StyleID="${style}"><Data ss:Type="${isNumber ? "Number" : "String"}">${xmlEscape(value)}</Data></Cell>`
}

function exportExcel(event: MouseEvent) {
  if (!props.rows.length || exporting.value) {
    event.preventDefault()
    return
  }
  exporting.value = true
  try {
    const exportRows = props.rows.slice(0, 5000)
    const columnWidths = props.columns.map((column) => {
      const maxLength = Math.max(column.length, ...exportRows.slice(0, 200).map((row) => String(row[column] ?? "").length))
      return Math.min(220, Math.max(78, maxLength * 13))
    })
    const columns = columnWidths.map((width) => `<Column ss:Width="${width}"/>`).join("")
    const titleRow = `<Row ss:Height="30"><Cell ss:StyleID="Title" ss:MergeAcross="${Math.max(0, props.columns.length - 1)}"><Data ss:Type="String">${xmlEscape(props.title)}</Data></Cell></Row>`
    const metaRow = `<Row><Cell ss:StyleID="Meta" ss:MergeAcross="${Math.max(0, props.columns.length - 1)}"><Data ss:Type="String">导出时间：${xmlEscape(new Date().toLocaleString("zh-CN"))} · ${props.rows.length} 行${props.truncated ? ' · 仅导出当前预览，非完整查询结果' : ''}</Data></Cell></Row>`
    const headerRow = `<Row>${props.columns.map((column) => excelCell(column, "Header")).join("")}</Row>`
    const dataRows = exportRows.map((row) => `<Row>${props.columns.map((column) => excelCell(row[column])).join("")}</Row>`).join("")
    const workbook = `<?xml version="1.0"?><?mso-application progid="Excel.Sheet"?>
      <Workbook xmlns="urn:schemas-microsoft-com:office:spreadsheet" xmlns:ss="urn:schemas-microsoft-com:office:spreadsheet">
        <Styles>
          <Style ss:ID="Default"><Alignment ss:Vertical="Center"/><Font ss:FontName="Microsoft YaHei" ss:Size="10"/></Style>
          <Style ss:ID="Title"><Font ss:FontName="Microsoft YaHei" ss:Size="16" ss:Bold="1" ss:Color="#173D2C"/><Interior ss:Color="#EAF5EE" ss:Pattern="Solid"/></Style>
          <Style ss:ID="Meta"><Font ss:FontName="Microsoft YaHei" ss:Size="9" ss:Color="#718078"/></Style>
          <Style ss:ID="Header"><Font ss:FontName="Microsoft YaHei" ss:Bold="1" ss:Color="#FFFFFF"/><Interior ss:Color="#246B4E" ss:Pattern="Solid"/><Borders><Border ss:Position="Bottom" ss:LineStyle="Continuous" ss:Weight="1" ss:Color="#DDE8E1"/></Borders></Style>
          <Style ss:ID="Data"><Borders><Border ss:Position="Bottom" ss:LineStyle="Continuous" ss:Weight="1" ss:Color="#E7ECE8"/></Borders></Style>
        </Styles>
        <Worksheet ss:Name="查询结果"><Table>${columns}${titleRow}${metaRow}${headerRow}${dataRows}</Table></Worksheet>
      </Workbook>`
    const blob = new Blob(["\ufeff", workbook], { type: "application/vnd.ms-excel;charset=utf-8" })
    const url = URL.createObjectURL(blob)
    const anchor = event.currentTarget as HTMLAnchorElement
    const safeTitle = props.title.replace(/[\\/:*?"<>|]/g, "_")
    anchor.href = url
    anchor.download = `${safeTitle}${props.truncated ? '_预览' : ''}_${new Date().toISOString().slice(0, 10)}.xls`
    window.setTimeout(() => {
      URL.revokeObjectURL(url)
      anchor.removeAttribute("href")
      anchor.removeAttribute("download")
      exporting.value = false
    }, 1000)
  } catch (error) {
    event.preventDefault()
    exporting.value = false
    throw error
  }
}
</script>

<template>
  <section class="data-card" aria-label="表格查询结果">
    <header class="data-card-header">
      <div class="data-card-heading">
        <span class="sheet-icon">▦</span>
        <div><strong>{{ title }}</strong><small>{{ rows.length.toLocaleString("zh-CN") }} 行 · {{ updatedAt }}</small></div>
      </div>
      <div class="data-card-actions">
        <slot name="actions"></slot>
        <a class="export-button" :class="{ disabled: !rows.length || exporting }" :aria-disabled="!rows.length || exporting" @click="exportExcel">
          <span>⇩</span>{{ exporting ? "正在导出" : (truncated ? "导出预览" : "导出 Excel") }}
        </a>
      </div>
    </header>

    <slot name="details"></slot>
    <p v-if="truncated" class="table-state" role="status">结果超过200行，目前只展示前200行。导出和解读仅覆盖当前预览，请缩小日期范围或增加筛选条件。</p>

    <div v-if="loading" class="table-state"><span class="state-spinner"></span><strong>正在加载表格数据…</strong></div>
    <div v-else-if="error" class="table-state error"><span>!</span><strong>{{ error }}</strong></div>
    <div v-else-if="!rows.length" class="table-state"><span>○</span><strong>当前没有可展示的数据</strong><small>调整查询条件后再试一次</small></div>
    <template v-else>
      <div class="data-table-wrap">
        <table class="data-table">
          <thead><tr><th v-for="column in columns" :key="column">{{ column }}</th></tr></thead>
          <tbody>
            <tr v-for="(row, rowIndex) in pagedRows" :key="rowIndex">
              <td v-for="column in columns" :key="column" :title="String(row[column] ?? '')">{{ formatCell(row[column], column) }}</td>
            </tr>
          </tbody>
        </table>
      </div>
      <footer class="data-card-footer">
        <label>每页
          <select v-model.number="pageSize" aria-label="每页显示数量"><option :value="10">10</option><option :value="20">20</option><option :value="50">50</option></select>
          行
        </label>
        <span>第 {{ page }} / {{ totalPages }} 页</span>
        <div class="page-actions">
          <button :disabled="page === 1" aria-label="上一页" @click="previousPage">‹</button>
          <button :disabled="page === totalPages" aria-label="下一页" @click="nextPage">›</button>
        </div>
      </footer>
    </template>
  </section>
</template>

<style scoped>
.data-card { overflow: hidden; border: 1px solid #dfe7e2; border-radius: 15px; background: #fff; box-shadow: 0 12px 34px rgba(34, 68, 49, .08); }
.data-card-header { display: flex; align-items: center; justify-content: space-between; gap: 16px; min-height: 64px; padding: 13px 16px; border-bottom: 1px solid #e9eeea; background: linear-gradient(135deg, #fbfdfb, #f4f9f6); }
.data-card-heading, .data-card-actions { display: flex; align-items: center; gap: 10px; }.data-card-heading { min-width: 0; }
.sheet-icon { display: grid; place-items: center; width: 34px; height: 34px; flex: none; border-radius: 10px; color: #24714f; background: #e3f1e8; font-size: 14px; }
.data-card-heading strong, .data-card-heading small { display: block; }.data-card-heading strong { overflow: hidden; color: #28382f; text-overflow: ellipsis; white-space: nowrap; font-size: 12px; }.data-card-heading small { margin-top: 4px; color: #8a968f; font-size: 8px; }
.data-card-actions :deep(button), .export-button { min-height: 30px; padding: 0 10px; border: 1px solid #dce4df; border-radius: 8px; color: #5f6d65; background: #fff; font-size: 8px; }
.export-button { display: flex; align-items: center; gap: 5px; border-color: #246b4e; color: #fff; background: #246b4e; cursor: pointer; font-weight: 600; text-decoration: none; }.export-button span { font-size: 12px; }.export-button.disabled { cursor: default; opacity: .45; }
.data-table-wrap { max-height: 430px; overflow: auto; }
.data-table { width: 100%; border-collapse: separate; border-spacing: 0; font-size: 10px; }
.data-table th { position: sticky; top: 0; z-index: 1; padding: 11px 14px; border-bottom: 1px solid #dfe6e1; color: #6e7b74; background: #f6f8f6; text-align: left; font-size: 8px; font-weight: 700; white-space: nowrap; }
.data-table td { max-width: 240px; padding: 11px 14px; overflow: hidden; border-bottom: 1px solid #edf0ee; color: #3f4b44; text-overflow: ellipsis; white-space: nowrap; }
.data-table tbody tr:nth-child(even) { background: #fbfcfb; }.data-table tbody tr:hover { background: #edf7f1; }
.data-card-footer { display: flex; align-items: center; min-height: 50px; gap: 16px; padding: 9px 14px; color: #7c8881; background: #fbfcfb; font-size: 8px; }
.data-card-footer label { display: flex; align-items: center; gap: 5px; }.data-card-footer select { padding: 4px 16px 4px 6px; border: 1px solid #dce3de; border-radius: 6px; color: #4e5e55; background: #fff; font-size: 8px; }
.data-card-footer > span { margin-left: auto; }.page-actions { display: flex; gap: 5px; }.page-actions button { display: grid; place-items: center; width: 28px; height: 28px; border: 1px solid #dbe3de; border-radius: 7px; color: #486057; background: #fff; font-size: 15px; }.page-actions button:disabled { opacity: .35; }
.table-state { display: flex; min-height: 180px; flex-direction: column; align-items: center; justify-content: center; gap: 7px; color: #87928c; }.table-state > span { display: grid; place-items: center; width: 30px; height: 30px; border-radius: 9px; background: #edf3ef; }.table-state strong { font-size: 10px; }.table-state small { font-size: 8px; }.table-state.error { color: #a45143; }
.state-spinner { border: 2px solid #dbe9e0; border-top-color: #277454; border-radius: 50% !important; animation: spin .8s linear infinite; }
@keyframes spin { to { transform: rotate(360deg); } }
@media (max-width: 620px) { .data-card-header { align-items: flex-start; flex-direction: column; }.data-card-actions { width: 100%; justify-content: flex-end; }.data-card-footer { gap: 8px; }.data-table td, .data-table th { padding: 10px; } }
</style>
