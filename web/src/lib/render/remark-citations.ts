import type { Parent, PhrasingContent, Root, RootContent, Text } from 'mdast'

// Turns evidence/finding/hypothesis refs in answer text into `citation` elements, which
// the Markdown renderer maps to CitationChip. Refs inside code, links or existing
// citations are left alone. "[ev_x, fd_y]" groups lose their brackets and become one
// citation group.

export const REF_SOURCE = '(?:ev|fd|hy)_[0-9a-f]{8}'
export const REF = new RegExp(`^${REF_SOURCE}$`)
const TOKENS = new RegExp(
  `\\[(${REF_SOURCE}(?:\\s*,\\s*${REF_SOURCE})*)\\]|\\b(${REF_SOURCE})\\b`,
  'g',
)
const SKIP = new Set(['link', 'linkReference', 'inlineCode', 'code', 'citation'])

type CitationNode = {
  type: 'citation'
  data: { hName: 'citation'; hChildren: [{ type: 'text'; value: string }] }
  children: []
}

function citation(ref: string): CitationNode {
  return {
    type: 'citation',
    data: { hName: 'citation', hChildren: [{ type: 'text', value: ref }] },
    children: [],
  }
}

function split(node: Text): (Text | CitationNode)[] | null {
  const out: (Text | CitationNode)[] = []
  let last = 0
  for (const match of node.value.matchAll(TOKENS)) {
    const index = match.index ?? 0
    if (index > last) out.push({ type: 'text', value: node.value.slice(last, index) })
    const refs = match[1] ? match[1].split(/\s*,\s*/) : [match[2]!]
    // One element per group, refs space-separated, so a long run can be shown compactly.
    out.push(citation(refs.join(' ')))
    last = index + match[0].length
  }
  if (out.length === 0) return null
  if (last < node.value.length) out.push({ type: 'text', value: node.value.slice(last) })
  return out
}

function walk(parent: Parent) {
  const next: RootContent[] = []
  for (const child of parent.children as RootContent[]) {
    if (child.type === 'text') {
      const parts = split(child)
      if (parts) {
        next.push(...(parts as unknown as PhrasingContent[]))
        continue
      }
    } else if (!SKIP.has(child.type) && 'children' in child) {
      walk(child as Parent)
    }
    next.push(child)
  }
  parent.children = next as Parent['children']
}

export function remarkCitations() {
  return (tree: Root) => walk(tree)
}
