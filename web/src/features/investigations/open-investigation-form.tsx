import { useId, useState, type FormEvent } from 'react'
import { useNavigate } from 'react-router'

import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { isUuid } from '@/lib/ids'

// Development-only stand-in for the investigation list (no list endpoint yet): open an
// investigation whose ID you know, e.g. from scripts/run_investigation.py.
export function OpenInvestigationForm() {
  const navigate = useNavigate()
  const inputId = useId()
  const errorId = useId()
  const [value, setValue] = useState('')
  const [error, setError] = useState<string | null>(null)

  function submit(event: FormEvent) {
    event.preventDefault()
    const id = value.trim()
    if (!isUuid(id)) {
      setError('Enter a valid investigation ID (UUID).')
      return
    }
    navigate(`/investigations/${id}`)
  }

  return (
    <form onSubmit={submit} className="flex w-full max-w-md flex-col gap-2 text-left" noValidate>
      <Label htmlFor={inputId}>Investigation ID</Label>
      <div className="flex gap-2">
        <Input
          id={inputId}
          value={value}
          onChange={(e) => {
            setValue(e.target.value)
            setError(null)
          }}
          placeholder="00000000-0000-0000-0000-000000000000"
          autoComplete="off"
          spellCheck={false}
          className="font-mono"
          aria-invalid={error ? true : undefined}
          aria-describedby={error ? errorId : undefined}
        />
        <Button type="submit">Open</Button>
      </div>
      {error && (
        <p id={errorId} className="text-sm text-destructive">
          {error}
        </p>
      )}
    </form>
  )
}
