import { useCallback, useEffect, useRef, useState } from 'react'

const NEAR_BOTTOM_PX = 160

function nearBottom() {
  const doc = document.documentElement
  return window.innerHeight + window.scrollY >= doc.scrollHeight - NEAR_BOTTOM_PX
}

function prefersReducedMotion() {
  return window.matchMedia?.('(prefers-reduced-motion: reduce)').matches ?? false
}

// Follows new activity like a chat: when the reader is at the bottom, new content keeps
// them there; when they scrolled up to read, it does not yank them back but offers a
// "jump to latest" control instead. `version` changes whenever content grows.
export function useStickToBottom(version: unknown, ready: boolean) {
  const following = useRef(true)
  const initial = useRef(true)
  const [unseen, setUnseen] = useState(false)

  // Scrolls to the document end, not to an element: the composer is pinned over the
  // bottom of the viewport, so aligning content to the viewport edge would leave the
  // newest lines hidden behind it. Automatic follows are instant (bursts of replayed
  // events would otherwise interrupt each other's smooth scroll).
  const jump = useCallback((smooth = true) => {
    window.scrollTo({
      top: document.documentElement.scrollHeight,
      behavior: smooth && !prefersReducedMotion() ? 'smooth' : 'auto',
    })
    following.current = true
    setUnseen(false)
  }, [])

  useEffect(() => {
    const onScroll = () => {
      following.current = nearBottom()
      if (following.current) setUnseen(false)
    }
    window.addEventListener('scroll', onScroll, { passive: true })
    return () => window.removeEventListener('scroll', onScroll)
  }, [])

  useEffect(() => {
    if (!ready) return
    if (initial.current) {
      initial.current = false
      jump(false)
      return
    }
    if (following.current) jump(false)
    else setUnseen(true)
  }, [version, ready, jump])

  return { unseen, jump }
}
