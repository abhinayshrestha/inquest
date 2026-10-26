import { useSearchParams } from 'react-router'

import { REF } from '@/lib/render/remark-citations'

// The open citation lives in the URL (?ref=ev_...), so it survives reload, can be
// linked, and the browser back button closes it.
export function useCitationParam() {
  const [params, setParams] = useSearchParams()
  const raw = params.get('ref')
  const ref = raw && REF.test(raw) ? raw : null
  const open = (next: string) =>
    setParams((p) => {
      p.set('ref', next)
      return p
    })
  const close = () =>
    setParams((p) => {
      p.delete('ref')
      return p
    })
  return { ref, open, close }
}
