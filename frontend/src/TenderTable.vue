<script setup>
import { computed } from 'vue'

const props = defineProps({
  table: { type: Object, required: true },
  editable: { type: Boolean, default: false },
  fillGaps: { type: Boolean, default: false },
})
const emit = defineEmits(['edit', 'input'])

const rows = computed(() => {
  if (!props.fillGaps) return props.table.rows

  const rowWidths = props.table.rows.map(row => row.reduce((width, cell) => width + (cell.colspan || 1), 0))
  const columns = rowWidths.reduce((maximum, width) => Math.max(maximum, width), props.table.grid_columns || 0)
  const blankCells = count => Array.from({ length: count }, () => ({ placeholder: true }))

  return props.table.rows.map((row, index) => [
    ...row.flatMap(cell => cell.placeholder ? blankCells(cell.colspan || 1) : [cell]),
    ...blankCells(columns - rowWidths[index]),
  ])
})
const columnWidths = computed(() => {
  const widths = props.table.column_widths || []
  const total = widths.reduce((sum, width) => sum + width, 0)
  return total ? widths.map(width => `${(width / total * 100).toFixed(3)}%`) : []
})
const tableStyle = computed(() => {
  const alignment = props.table.word_alignment
  return {
    width: props.table.word_width || undefined,
    tableLayout: columnWidths.value.length ? 'fixed' : undefined,
    marginInlineStart: ['center', 'right', 'end'].includes(alignment) ? 'auto' : undefined,
    marginInlineEnd: alignment === 'center' ? 'auto' : undefined,
  }
})

function cellStyle(cell, columnIndex) {
  return cell.word_style || { textAlign: props.table.column_alignments?.[columnIndex] }
}
</script>

<template>
  <div class="word-edit-table-wrap">
    <table class="word-edit-table" :style="tableStyle">
      <colgroup v-if="columnWidths.length">
        <col v-for="(width, index) in columnWidths" :key="index" :style="{ width }" />
      </colgroup>
      <tbody>
        <tr v-for="(row, rowIndex) in rows" :key="rowIndex" :class="{'word-table-header':rowIndex===0}">
          <template v-for="(cell, cellIndex) in row" :key="cellIndex">
            <td v-if="cell.display !== false" :colspan="cell.colspan || 1" :rowspan="cell.rowspan || 1" :style="cellStyle(cell, cellIndex)">
              <template v-if="!cell.placeholder">
                <div v-if="editable"
                     contenteditable="plaintext-only"
                     spellcheck="false"
                     class="word-edit-table-cell"
                     data-table-editor
                     @input="emit('input')"
                     @blur="emit('edit', cell, $event.currentTarget.innerText)"
                     v-text="cell.text"></div>
                <div v-else class="word-table-text">{{ cell.text }}</div>
                <TenderTable v-for="nested in cell.nested_tables" :key="nested.table_index" :table="nested" :fill-gaps="fillGaps" />
              </template>
            </td>
          </template>
        </tr>
      </tbody>
    </table>
  </div>
</template>
