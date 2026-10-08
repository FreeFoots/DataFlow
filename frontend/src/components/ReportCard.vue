<script setup lang="ts">
import { computed, ref } from "vue"
import type { AnalysisReport, ReportDataSource } from "../types"
import AnalysisChart from "./AnalysisChart.vue"
import MarkdownReport from "./MarkdownReport.vue"

const props = defineProps<{
  report: AnalysisReport
  sources: ReportDataSource[]
}>()
const reportElement = ref<HTMLElement | null>(null)

const displayMarkdown = computed(() => {
  const lines = props.report.markdown.replace(/\r\n/g, "\n").split("\n")
  const firstContent = lines.findIndex((line) => line.trim())
  if (firstContent >= 0) {
    const heading = lines[firstContent].trim().match(/^#\s+(.+)$/)
    if (heading?.[1].trim() === props.report.title.trim()) {
      lines.splice(firstContent, 1)
    }
  }
  return lines.join("\n").trim()
})

function exportMarkdown() {
  const blob = new Blob([props.report.markdown], { type: "text/markdown;charset=utf-8" })
  const url = URL.createObjectURL(blob)
  const anchor = document.createElement("a")
  anchor.href = url
  anchor.download = `${props.report.title.replace(/[\\/:*?\"<>|]/g, "-") || "数据分析报告"}.md`
  anchor.click()
  URL.revokeObjectURL(url)
}

function exportPdf() {
  if (!reportElement.value) return

  const printable = reportElement.value.cloneNode(true) as HTMLElement
  printable.classList.add("pdf-print-root")
  printable.querySelectorAll(".report-actions").forEach((element) => element.remove())

  const previousTitle = document.title
  let cleaned = false
  const cleanup = () => {
    if (cleaned) return
    cleaned = true
    printable.remove()
    document.body.classList.remove("printing-report")
    document.title = previousTitle
    window.removeEventListener("afterprint", cleanup)
  }

  document.title = props.report.title.replace(/[\\/:*?"<>|]/g, "-") || "数据分析报告"
  document.body.appendChild(printable)
  document.body.classList.add("printing-report")
  window.addEventListener("afterprint", cleanup, { once: true })

  requestAnimationFrame(() => {
    try {
      window.print()
    } finally {
      window.setTimeout(cleanup, 100)
    }
  })
}
</script>

<template>
  <article ref="reportElement" class="report-card">
    <header class="report-header">
      <div><small>MARKDOWN REPORT</small><h2>{{ report.title }}</h2></div>
      <div class="report-actions">
        <button @click="exportMarkdown"><span>↓</span> 导出 .md</button>
        <button class="pdf-export" title="打开系统打印窗口并保存为PDF" @click="exportPdf"><span>▣</span> 导出 PDF</button>
      </div>
    </header>
    <div v-if="report.visualizations.length" class="report-charts">
      <AnalysisChart v-for="(spec, index) in report.visualizations" :key="`${spec.source_task_id}:${index}`" :spec="spec" :sources="sources" />
    </div>
    <MarkdownReport :markdown="displayMarkdown" />
  </article>
</template>
