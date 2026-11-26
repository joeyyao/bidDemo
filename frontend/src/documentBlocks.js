export const previewHeading = /^(第(?:[一二三四五六七八九十百0-9]+|X(?:\+\d+)?)[章节条]|[一二三四五六七八九十]+[、.]|\d+(?:\.\d+){0,3}[、.． ]|（[一二三四五六七八九十]+）)/
const levelOneHeading = /^(?:\d+[、.．](?!\d)\s*\S|第(?:[一二三四五六七八九十百0-9]+|X(?:\+\d+)?)[章节条]\s*\S|[一二三四五六七八九十]+[、.]\s*\S)/
const numberedSubitem = /^\d+(?:\.\d+)+(?:[、.．]|\s|$)/

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
      const lines = paragraph.split('\n').map(line => line.trim()).filter(Boolean)
      let body = []
      const flushBody = () => {
        if (body.length) blocks.push({ kind: 'paragraph', text: body.join('\n') })
        body = []
      }
      for (const line of lines) {
        if (levelOneHeading.test(line)) {
          flushBody()
          blocks.push({ kind: 'heading', text: line })
        } else if (numberedSubitem.test(line)) {
          flushBody()
          body.push(line)
        } else {
          body.push(line)
        }
      }
      flushBody()
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

const numberedText = /^(?:\d+(?:\.\d+)*[、.．)]?|第(?:[一二三四五六七八九十百0-9]+|X(?:\+\d+)?)[章节条]|[一二三四五六七八九十]+[、.])/

export function normalizeNumberedBlocks(blocks) {
  return blocks.flatMap(block => {
    if (!['paragraph', 'heading', 'subheading'].includes(block.kind)) return [block]
    if (!numberedText.test((block.text || '').trim())) return restoreDraftTables([block])
    return restoreDraftTables(contentBlocks(block.text))
  })
}
