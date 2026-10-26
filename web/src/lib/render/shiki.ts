import type { Root } from 'hast'
import type { HighlighterCore } from 'shiki/core'

// One lazily created highlighter for the app: a fixed set of languages and two themes,
// loaded on first use so pages without code never download it. The JavaScript regex
// engine avoids shipping and instantiating a WASM module.

export const LANGUAGES = ['bash', 'json', 'python', 'sql', 'typescript', 'yaml'] as const
const ALIASES: Record<string, (typeof LANGUAGES)[number]> = {
  sh: 'bash',
  shell: 'bash',
  zsh: 'bash',
  ts: 'typescript',
  js: 'typescript',
  javascript: 'typescript',
  yml: 'yaml',
  py: 'python',
}

let highlighter: Promise<HighlighterCore> | null = null

function load(): Promise<HighlighterCore> {
  highlighter ??= (async () => {
    const [{ createHighlighterCore }, { createJavaScriptRegexEngine }] = await Promise.all([
      import('shiki/core'),
      import('shiki/engine/javascript'),
    ])
    return createHighlighterCore({
      themes: [import('shiki/themes/github-light.mjs'), import('shiki/themes/github-dark.mjs')],
      langs: [
        import('shiki/langs/bash.mjs'),
        import('shiki/langs/json.mjs'),
        import('shiki/langs/python.mjs'),
        import('shiki/langs/sql.mjs'),
        import('shiki/langs/typescript.mjs'),
        import('shiki/langs/yaml.mjs'),
      ],
      engine: createJavaScriptRegexEngine({ forgiving: true }),
    })
  })()
  return highlighter
}

export function resolveLanguage(language: string): string | null {
  const lang = language.toLowerCase()
  if ((LANGUAGES as readonly string[]).includes(lang)) return lang
  return ALIASES[lang] ?? null
}

// Tokens only: the result is a hast tree of spans with color variables, rendered as React
// elements by the caller. Unknown languages are not highlighted (null).
export async function highlight(code: string, language: string): Promise<Root | null> {
  const lang = resolveLanguage(language)
  if (!lang) return null
  const h = await load()
  return h.codeToHast(code, {
    lang,
    themes: { light: 'github-light', dark: 'github-dark' },
    defaultColor: false,
  })
}
