const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i

// Backend IDs are UUIDs; anything else cannot exist, so it is not worth a request.
export function isUuid(value: string | undefined): value is string {
  return value !== undefined && UUID.test(value)
}
