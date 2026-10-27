// Shared so SSE-driven invalidation and the pages agree on keys. Collections are keyed by
// organization; everything tenant-specific is dropped when the organization changes or the
// user signs out (TENANT_ROOTS).
export const queryKeys = {
  health: ['health'] as const,
  overview: (organizationId: string) => ['overview', organizationId] as const,
  reports: (organizationId: string) => ['reports', organizationId] as const,
  environments: (organizationId: string) => ['environments', organizationId] as const,
  environment: (id: string) => ['environment', id] as const,
  telemetry: (id: string) => ['environment', id, 'telemetry'] as const,
  investigations: ['investigations'] as const,
  investigationList: (organizationId: string) => ['investigations', organizationId] as const,
  investigation: (id: string) => ['investigation', id] as const,
  citation: (id: string, ref: string) => ['citation', id, ref] as const,
  presentation: (id: string, turnId: string) => ['presentation', id, turnId] as const,
  report: (id: string, version: number) => ['report', id, version] as const,
}

export const TENANT_ROOTS: ReadonlySet<unknown> = new Set([
  'overview',
  'reports',
  'environments',
  'environment',
  'investigations',
  'investigation',
  'citation',
  'presentation',
  'report',
])
