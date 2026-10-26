const SAFE_PROTOCOL = /^(https?:|mailto:)/i

// Links in model-written Markdown may only leave to the web or mail; javascript:, data:,
// relative and app-internal URLs are dropped (the link renders as plain text).
export function safeUrl(url: string): string {
  return SAFE_PROTOCOL.test(url.trim()) ? url : ''
}
