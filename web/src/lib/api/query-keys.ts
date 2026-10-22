// Shared so SSE-driven invalidation (Phase 6B) and the pages agree on keys.
export const queryKeys = {
  health: ['health'] as const,
  investigation: (id: string) => ['investigation', id] as const,
  report: (id: string, version: number) => ['report', id, version] as const,
}
