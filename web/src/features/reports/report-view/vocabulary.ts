import type { Claim } from '@/lib/api'

// What a conclusion asserts, from weakest to strongest (validation.CLAIMS in the backend).
// Shown with every conclusion so a co-occurrence is never read as a cause.
export const CLAIM_INFO: Record<Claim | 'unclassified', { label: string; meaning: string }> = {
  observation: {
    label: 'Observation',
    meaning: 'Something measured in the evidence. No relationship to anything else is claimed.',
  },
  temporal_overlap: {
    label: 'Overlap in time',
    meaning: 'Two observations fall in the same window. Happening together is not a relationship.',
  },
  correlation: {
    label: 'Correlation',
    meaning: 'Two signals move together over time. That does not show that one causes the other.',
  },
  causal: {
    label: 'Causal',
    meaning: 'One thing causes another. Only accepted with trace evidence of the path.',
  },
  unclassified: {
    label: 'Not classified',
    meaning: 'Written before conclusions declared a claim level; read it as an observation.',
  },
}

export const CLAIM_ORDER: (Claim | 'unclassified')[] = [
  'causal',
  'correlation',
  'temporal_overlap',
  'observation',
  'unclassified',
]

export const OUTCOME_LABELS: Record<string, string> = {
  root_cause_identified: 'Root cause identified',
  probable_cause: 'Probable cause',
  inconclusive: 'Inconclusive',
}

export const STATUS_TONE: Record<string, 'success' | 'info' | 'warning' | 'danger' | 'neutral'> = {
  confirmed: 'success',
  probable: 'info',
  unverified: 'warning',
  ruled_out: 'neutral',
  retracted: 'danger',
}
