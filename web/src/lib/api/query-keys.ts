// Shared so SSE-driven invalidation and the pages agree on keys.
export const queryKeys = {
  health: ['health'] as const,
  environments: ['environments'] as const,
  investigations: ['investigations'] as const,
  investigation: (id: string) => ['investigation', id] as const,
  citation: (id: string, ref: string) => ['citation', id, ref] as const,
  presentation: (id: string, turnId: string) => ['presentation', id, turnId] as const,
  report: (id: string, version: number) => ['report', id, version] as const,
}
