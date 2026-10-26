import { FileSearch, Lightbulb, ListChecks } from 'lucide-react'

const KIND = {
  ev: { label: 'Evidence', icon: FileSearch },
  fd: { label: 'Finding', icon: ListChecks },
  hy: { label: 'Hypothesis', icon: Lightbulb },
} as const

export function citationKind(ref: string) {
  return KIND[ref.slice(0, 2) as keyof typeof KIND] ?? KIND.ev
}
