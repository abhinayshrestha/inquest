import { ExternalLink } from 'lucide-react'
import type { Element, ElementContent } from 'hast'
import type { ComponentProps, ReactNode } from 'react'
import ReactMarkdown, { type Components } from 'react-markdown'
import remarkGfm from 'remark-gfm'

import { cn } from 'cn'

import { CitationGroup } from './citation-group'
import { CodeView } from './code-view'
import { reflowMarkdown } from './reflow'
import { REF, remarkCitations } from './remark-citations'
import { safeUrl } from './safe-url'

// Model-written Markdown is rendered as data: no raw HTML (skipHtml, no rehype-raw), only
// http(s)/mailto links, no remote images (they could track or leak), and fenced code is
// highlighted by Shiki's tokenizer, never executed. Refs become citation chips. Line breaks
// lost by the model are restored first (reflow.ts).

function Anchor({ href, children }: ComponentProps<'a'>) {
  if (!href) return <span>{children}</span>
  return (
    <a
      href={href}
      target="_blank"
      rel="noopener noreferrer nofollow"
      className="inline-flex items-center gap-0.5 text-primary underline underline-offset-2"
    >
      {children}
      <ExternalLink aria-hidden className="size-3" />
      <span className="sr-only">(opens in a new tab)</span>
    </a>
  )
}

function textOf(nodes: ElementContent[]): string {
  return nodes
    .map((n) => (n.type === 'text' ? n.value : n.type === 'element' ? textOf(n.children) : ''))
    .join('')
}

function components(onCite?: (ref: string) => void): Components {
  return {
    a: Anchor,
    img: ({ alt }) => <span className="text-muted-foreground">[image: {alt || 'not shown'}]</span>,
    table: ({ children }) => (
      <div className="my-3 overflow-x-auto rounded-md border">
        <table className="w-full border-collapse text-sm">{children}</table>
      </div>
    ),
    th: ({ children, style }) => (
      <th style={style} className="border-b bg-muted px-3 py-1.5 text-left font-medium">
        {children}
      </th>
    ),
    td: ({ children, style }) => (
      <td style={style} className="border-b px-3 py-1.5 align-top last:border-b-0">
        {children}
      </td>
    ),
    // Fenced code goes through Shiki (CodeView); the hast node gives the raw text and
    // language without re-parsing rendered children.
    pre: ({ node }) => {
      const code = node?.children.find(
        (n): n is Element => n.type === 'element' && n.tagName === 'code',
      )
      const classes = (code?.properties.className as string[] | undefined) ?? []
      const language = classes.find((c) => c.startsWith('language-'))?.slice(9) ?? ''
      return <CodeView code={textOf(code?.children ?? []).replace(/\n$/, '')} language={language} />
    },
    // Inline code only: fenced code never reaches here (pre renders CodeView).
    code: ({ children }) => (
      <code className="rounded bg-muted px-1 py-0.5 font-mono text-[0.85em]">{children}</code>
    ),
    // Produced only by remarkCitations: space-separated refs of one group. Anything that
    // is not a ref renders as text.
    citation: ({ children }: { children?: ReactNode }) => {
      const tokens = String(children ?? '').split(' ')
      const refs = tokens.filter((t) => REF.test(t))
      return refs.length === tokens.length ? (
        <CitationGroup refs={refs} onSelect={onCite} />
      ) : (
        <>{tokens.join(' ')}</>
      )
    },
  } as Components
}

export function Markdown({
  children,
  onCite,
  className,
}: {
  children: string
  onCite?: (ref: string) => void
  className?: string
}) {
  return (
    <div
      className={cn(
        'text-sm leading-relaxed break-words [&>*+*]:mt-3',
        '[&_h1]:mt-5 [&_h1]:text-lg [&_h1]:font-semibold [&_h1]:tracking-tight',
        '[&_h2]:mt-5 [&_h2]:text-base [&_h2]:font-semibold [&_h2]:tracking-tight',
        '[&_h3]:mt-4 [&_h3]:text-sm [&_h3]:font-semibold [&_h4]:mt-3 [&_h4]:font-medium',
        '[&>h1:first-child]:mt-0 [&>h2:first-child]:mt-0 [&>h3:first-child]:mt-0',
        '[&_ol]:list-decimal [&_ol]:space-y-1.5 [&_ol]:pl-5 [&_ul]:list-disc [&_ul]:space-y-1.5 [&_ul]:pl-5',
        '[&_li]:pl-0.5 [&_li]:marker:text-muted-foreground [&_li>p]:mt-0',
        '[&_hr]:my-4 [&_strong]:font-semibold',
        '[&_blockquote]:border-l-2 [&_blockquote]:pl-3 [&_blockquote]:text-muted-foreground',
        className,
      )}
    >
      <ReactMarkdown
        skipHtml
        urlTransform={safeUrl}
        remarkPlugins={[remarkGfm, remarkCitations]}
        components={components(onCite)}
      >
        {reflowMarkdown(children)}
      </ReactMarkdown>
    </div>
  )
}
