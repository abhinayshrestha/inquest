// Nemotron's compose output (function_calling, D021) arrives with its line breaks turned
// into runs of spaces: "…UTC)  [ev_…]  ### Slow endpoints  Three groups…:  - **GET**…".
// Rendered as-is, headings, lists and paragraphs collapse into one paragraph. Stored
// answers are immutable, so this restores the breaks at display time.
//
// Only lines that clearly contain glued-on Markdown blocks (two or more spaces after
// text, then a heading, bullet or numbered item) are touched; inside them every such run
// of spaces becomes a paragraph break. Fenced code, normal Markdown and plain prose with
// double-spaced sentences are left exactly as they are.

const GLUED_BLOCK = /\S {2,}(?:#{1,6} |[-*•] |\d{1,2}[.)] )/
const SPACE_RUN = /(\S) {2,}(?=\S)/g
const BULLET = /^• /gm

function reflowLine(line: string): string {
  if (!GLUED_BLOCK.test(line)) return line
  return line.replace(SPACE_RUN, '$1\n\n').replace(BULLET, '- ')
}

export function reflowMarkdown(markdown: string): string {
  let inFence = false
  return markdown
    .split('\n')
    .map((line) => {
      if (/^\s*(```|~~~)/.test(line)) {
        inFence = !inFence
        return line
      }
      return inFence ? line : reflowLine(line)
    })
    .join('\n')
}
