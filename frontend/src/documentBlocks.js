export const previewHeading = /^(第[一二三四五六七八九十百0-9]+[章节条]|[一二三四五六七八九十]+[、.]|\d+(?:\.\d+){0,3}[、.． ]|（[一二三四五六七八九十]+）)/

function markdownCells(line = '') {
  const trimmed = line.trim().replace(/^\|/, '').replace(/(?<!\\)\|$/, '')
  return trimmed.split(/(?<!\\)\|/).map(cell => cell.trim().replace(/\\\|/g, '|'))
}

function tableSeparator(line, columns) {
  const cells = markdownCells(line)
  return columns >= 2 && cells.length === columns && cells.every(cell => /^:?-{3,}:?$/.test(cell)) ? cells : null
}

export function contentBlocks(value) {
  const lines = (value || '').split(/\r?\n/)
  const blocks = []
  let text = []

  function flushText() {
    for (const paragraph of text.join('\n').split(/\n\s*\n/)) {
      const trimmed = paragraph.trim()
      if (trimmed) blocks.push({ kind: previewHeading.test(trimmed) ? 'heading' : 'paragraph', text: trimmed })
    }
    text = []
  }

  for (let index = 0; index < lines.length; index++) {
    const header = markdownCells(lines[index])
    const separator = tableSeparator(lines[index + 1], header.length)
    if (!separator) {
      text.push(lines[index])
      continue
    }
    flushText()
    const column_alignments = separator.map(cell => cell.endsWith(':') ? cell.startsWith(':') ? 'center' : 'right' : 'left')
    const rows = [header]
    index += 2
    while (index < lines.length && /(?<!\\)\|/.test(lines[index])) {
      const cells = markdownCells(lines[index])
      if (cells.length > header.length || tableSeparator(lines[index + 1], cells.length)) break
      rows.push(Array.from({ length: header.length }, (_, column) => cells[column] ?? ''))
      index++
    }
    index--
    blocks.push({ kind: 'table', header_row: true, column_alignments, rows: rows.map(row => row.map(text => ({ text }))) })
  }
  flushText()
  return blocks
}

export function restoreDraftTables(blocks) {
  return blocks.flatMap(block => {
    if (block.kind !== 'paragraph' && block.kind !== 'heading') return [block]
    const parsed = contentBlocks(block.text)
    return parsed.some(item => item.kind === 'table') ? parsed : [block]
  })
}
