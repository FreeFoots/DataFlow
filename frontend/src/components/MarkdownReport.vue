<script setup lang="ts">
import { computed } from "vue"

interface MarkdownBlock {
  type: "heading" | "paragraph" | "list" | "table" | "quote" | "rule"
  level?: number
  ordered?: boolean
  text?: string
  items?: string[]
  headers?: string[]
  rows?: string[][]
}

const props = defineProps<{ markdown: string }>()

function inline(text: string) {
  return text
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replace(/`([^`]+)`/g, "<code>$1</code>")
    .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
    .replace(/\*([^*]+)\*/g, "<em>$1</em>")
}

function tableCells(line: string) {
  return line.trim().replace(/^\||\|$/g, "").split("|").map((cell) => cell.trim())
}

function parseMarkdown(markdown: string) {
  const lines = markdown.replace(/\r\n/g, "\n").split("\n")
  const blocks: MarkdownBlock[] = []
  let index = 0
  while (index < lines.length) {
    const line = lines[index].trim()
    if (!line) { index += 1; continue }
    if (/^---+$/.test(line)) { blocks.push({ type: "rule" }); index += 1; continue }

    const heading = line.match(/^(#{1,3})\s+(.+)$/)
    if (heading) {
      blocks.push({ type: "heading", level: heading[1].length, text: heading[2] })
      index += 1
      continue
    }

    if (line.startsWith("|") && index + 1 < lines.length && /^\s*\|?[\s:|-]+\|?\s*$/.test(lines[index + 1])) {
      const headers = tableCells(line)
      index += 2
      const rows: string[][] = []
      while (index < lines.length && lines[index].trim().startsWith("|")) {
        rows.push(tableCells(lines[index]))
        index += 1
      }
      blocks.push({ type: "table", headers, rows })
      continue
    }

    const list = line.match(/^([-*]|\d+\.)\s+(.+)$/)
    if (list) {
      const ordered = /\d+\./.test(list[1])
      const items: string[] = []
      while (index < lines.length) {
        const item = lines[index].trim().match(/^([-*]|\d+\.)\s+(.+)$/)
        if (!item || /\d+\./.test(item[1]) !== ordered) break
        items.push(item[2])
        index += 1
      }
      blocks.push({ type: "list", ordered, items })
      continue
    }

    if (line.startsWith("> ")) {
      blocks.push({ type: "quote", text: line.slice(2) })
      index += 1
      continue
    }

    const paragraphs = [line]
    index += 1
    while (index < lines.length && lines[index].trim() && !/^(#{1,3})\s+|^([-*]|\d+\.)\s+|^>\s+|^\|/.test(lines[index].trim())) {
      paragraphs.push(lines[index].trim())
      index += 1
    }
    blocks.push({ type: "paragraph", text: paragraphs.join(" ") })
  }
  return blocks
}

const blocks = computed(() => parseMarkdown(props.markdown))
</script>

<template>
  <div class="markdown-body">
    <template v-for="(block, index) in blocks" :key="index">
      <h1 v-if="block.type === 'heading' && block.level === 1" v-html="inline(block.text || '')"></h1>
      <h2 v-else-if="block.type === 'heading' && block.level === 2" v-html="inline(block.text || '')"></h2>
      <h3 v-else-if="block.type === 'heading'" v-html="inline(block.text || '')"></h3>
      <p v-else-if="block.type === 'paragraph'" v-html="inline(block.text || '')"></p>
      <blockquote v-else-if="block.type === 'quote'" v-html="inline(block.text || '')"></blockquote>
      <hr v-else-if="block.type === 'rule'">
      <ol v-else-if="block.type === 'list' && block.ordered">
        <li v-for="item in block.items" :key="item" v-html="inline(item)"></li>
      </ol>
      <ul v-else-if="block.type === 'list'">
        <li v-for="item in block.items" :key="item" v-html="inline(item)"></li>
      </ul>
      <div v-else-if="block.type === 'table'" class="markdown-table-wrap">
        <table>
          <thead><tr><th v-for="header in block.headers" :key="header" v-html="inline(header)"></th></tr></thead>
          <tbody><tr v-for="(row, rowIndex) in block.rows" :key="rowIndex"><td v-for="(cell, cellIndex) in row" :key="cellIndex" v-html="inline(cell)"></td></tr></tbody>
        </table>
      </div>
    </template>
  </div>
</template>
