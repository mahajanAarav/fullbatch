import { useState, type FormEvent } from 'react'
import { createDrop } from '../api'
import { Modal } from './Modal'

export interface DropDraft {
  item_name?: string | null
  unit_price?: number | null
  quantity_total?: number
  minimum_units?: number
  max_per_buyer?: number
  deadline?: string // ISO 8601
}

const pad = (n: number) => String(n).padStart(2, '0')
// The value a datetime-local input wants, in the browser's own timezone.
function toLocalInput(d: Date): string {
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`
}

// Default deadline: tomorrow at 6 pm, in the seller's own timezone.
function defaultDeadline(): string {
  const d = new Date()
  d.setDate(d.getDate() + 1)
  d.setHours(18, 0, 0, 0)
  return toLocalInput(d)
}

export function CreateDropDialog({ open, draft, onClose, onCreated }: { open: boolean; draft?: DropDraft; onClose: () => void; onCreated: () => void }) {
  return (
    <Modal open={open} onClose={onClose} title="New drop">
      {open && <Form draft={draft} onClose={onClose} onCreated={onCreated} />}
    </Modal>
  )
}

function Form({ draft, onClose, onCreated }: { draft?: DropDraft; onClose: () => void; onCreated: () => void }) {
  const [item, setItem] = useState(draft?.item_name ?? '')
  const [price, setPrice] = useState(draft?.unit_price != null ? draft.unit_price.toFixed(2) : '')
  const [quantity, setQuantity] = useState(String(draft?.quantity_total ?? 12))
  const [minimum, setMinimum] = useState(String(draft?.minimum_units ?? 6))
  const [maxPer, setMaxPer] = useState(String(draft?.max_per_buyer ?? 4))
  const [deadline, setDeadline] = useState(() => (draft?.deadline ? toLocalInput(new Date(draft.deadline)) : defaultDeadline()))
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  async function submit(e: FormEvent) {
    e.preventDefault()
    if (busy) return
    const when = new Date(deadline)
    if (Number.isNaN(when.getTime())) return setError('Pick a deadline.')
    setBusy(true)
    setError(null)
    try {
      await createDrop({
        item_name: item.trim(),
        unit_price: Number(price).toFixed(2),
        quantity_total: Number(quantity),
        minimum_units: Number(minimum),
        max_per_buyer: Number(maxPer),
        deadline: when.toISOString(),
      })
      onCreated()
      onClose()
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Could not create the drop.')
      setBusy(false)
    }
  }

  return (
    <form className="form" onSubmit={submit}>
      <label>
        Item
        <input value={item} onChange={(e) => setItem(e.target.value)} placeholder="Sourdough loaf" required maxLength={200} autoFocus />
      </label>
      <div className="field-grid">
        <label>
          Price each ($)
          <input type="number" inputMode="decimal" min="0.01" step="0.01" value={price} onChange={(e) => setPrice(e.target.value)} placeholder="9.00" required />
        </label>
        <label>
          Quantity available
          <input type="number" min="1" step="1" value={quantity} onChange={(e) => setQuantity(e.target.value)} required />
        </label>
        <label>
          Minimum to run
          <input type="number" min="1" step="1" value={minimum} onChange={(e) => setMinimum(e.target.value)} required />
        </label>
        <label>
          Max per buyer
          <input type="number" min="1" step="1" value={maxPer} onChange={(e) => setMaxPer(e.target.value)} required />
        </label>
      </div>
      <label>
        Orders close
        <input type="datetime-local" value={deadline} onChange={(e) => setDeadline(e.target.value)} required />
      </label>
      <p className="note">
        If fewer than the minimum are approved by the deadline, every PayPal hold is released and nobody is charged. Drops can run up to 14 days.
      </p>
      {error && <p className="error" role="alert">{error}</p>}
      <div className="form-actions">
        <button type="button" className="button button-ghost" onClick={onClose}>
          Cancel
        </button>
        <button className="button" disabled={busy || !item.trim() || !price}>
          {busy ? 'Creating…' : 'Create drop'}
        </button>
      </div>
    </form>
  )
}
