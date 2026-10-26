import type { Element, Root } from 'hast'
import { toJsxRuntime } from 'hast-util-to-jsx-runtime'
import { useEffect, useState } from 'react'
import { Fragment, jsx, jsxs } from 'react/jsx-runtime'

import { cn } from 'cn'

import { highlight } from './shiki'

// Fenced code from answers. Shown as plain text immediately; replaced by Shiki's tokens
// once highlighted. Tokens become React elements (no HTML strings), so model-written code
// can only ever be displayed, never interpreted.

function codeChildren(root: Root) {
  // Shiki returns <pre class="shiki"><code>…</code></pre>; keep only the token lines so our
  // own <pre> controls layout.
  const pre = root.children.find((n): n is Element => n.type === 'element' && n.tagName === 'pre')
  const code = pre?.children.find((n): n is Element => n.type === 'element' && n.tagName === 'code')
  return code
    ? toJsxRuntime({ type: 'root', children: code.children }, { Fragment, jsx, jsxs })
    : null
}

export function CodeView({
  code,
  language,
  className,
}: {
  code: string
  language: string
  className?: string
}) {
  const [tokens, setTokens] = useState<{ key: string; node: React.ReactNode } | null>(null)
  const key = `${language}\u0000${code}`

  useEffect(() => {
    let cancelled = false
    highlight(code, language)
      .then((root) => {
        if (!cancelled && root) setTokens({ key, node: codeChildren(root) })
      })
      .catch(() => {
        // Highlighting is cosmetic; plain text stays.
      })
    return () => {
      cancelled = true
    }
  }, [code, language, key])

  const highlighted = tokens?.key === key ? tokens.node : null
  return (
    <div
      className={cn('group relative my-3 overflow-hidden rounded-md border bg-muted/40', className)}
    >
      {language && (
        <span className="absolute top-1.5 right-2 font-mono text-[0.65rem] text-muted-foreground uppercase">
          {language}
        </span>
      )}
      <pre className="overflow-x-auto p-3 text-xs leading-relaxed">
        <code className="shiki-code font-mono" data-highlighted={highlighted ? 'true' : 'false'}>
          {highlighted ?? code}
        </code>
      </pre>
    </div>
  )
}
