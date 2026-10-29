import { z } from 'zod'

// Zod would otherwise probe `new Function` to compile parsers, when the first schema is
// built; the Content-Security-Policy forbids eval, so the probe only fails and reports a
// violation (D041). Schema modules import z from here so this runs before any of them.
z.config({ jitless: true })

export { z }
