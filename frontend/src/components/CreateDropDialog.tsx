import { useState, type FormEvent } from 'react'
import { checkAddress, createDrop } from '../api'
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

// A seller usually sells from the same place every time, so remember it (in this browser only).
const PLACE_KEY = 'fullbatch.sellerPlace'
interface SavedPlace {
  address: string
  notes: string
  delivers: boolean
  radius: string
  fee: string
}
function savedPlace(): SavedPlace {
  const blank = { address: '', notes: '', delivers: false, radius: '5', fee: '3.00' }
  try {
    return { ...blank, ...JSON.parse(localStorage.getItem(PLACE_KEY) ?? '{}') }
  } catch {
    return blank
  }
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
  const saved = savedPlace()
  const [address, setAddress] = useState(saved.address)
  const [notes, setNotes] = useState(saved.notes)
  const [delivers, setDelivers] = useState(saved.delivers)
  const [radius, setRadius] = useState(saved.radius)
  const [fee, setFee] = useState(saved.fee)
  const [found, setFound] = useState<string | null>(null) // how the address was understood
  const [checking, setChecking] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  async function check() {
    setChecking(true)
    setError(null)
    setFound(null)
    try {
      setFound((await checkAddress(address.trim())).area)
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Could not check that address.')
    } finally {
      setChecking(false)
    }
  }

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
        pickup_address: address.trim(),
        pickup_notes: notes.trim() || null,
        offers_pickup: true,
        offers_delivery: delivers,
        delivery_radius_miles: delivers ? Number(radius) : null,
        delivery_fee: delivers ? Number(fee).toFixed(2) : '0.00',
      })
      try {
        localStorage.setItem(PLACE_KEY, JSON.stringify({ address: address.trim(), notes: notes.trim(), delivers, radius, fee }))
      } catch {
        /* not essential */
      }
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

      <fieldset className="where">
        <legend>Where do buyers get it?</legend>
        <label>
          Pickup address
          <div className="inline-field">
            <input
              value={address}
              onChange={(e) => {
                setAddress(e.target.value)
                setFound(null)
              }}
              placeholder="281 7th Ave, Brooklyn, NY 11215"
              required
              maxLength={300}
            />
            <button type="button" className="button button-ghost button-small" onClick={check} disabled={checking || address.trim().length < 3}>
              {checking ? 'Checking…' : 'Check'}
            </button>
          </div>
        </label>
        {found && (
          <p className="found">
            ✓ Buyers will see <strong>{found}</strong>. The exact address is shared only after they approve their payment.
          </p>
        )}
        <label>
          Pickup notes <span className="muted">(shared after they approve)</span>
          <input value={notes} onChange={(e) => setNotes(e.target.value)} placeholder="Ring the side bell. Pickup is on the porch." maxLength={300} />
        </label>
        <label className="check">
          <input type="checkbox" checked={delivers} onChange={(e) => setDelivers(e.target.checked)} />I also deliver
        </label>
        {delivers && (
          <div className="field-grid">
            <label>
              Delivery distance (miles)
              <input type="number" min="0.5" max="31" step="0.5" value={radius} onChange={(e) => setRadius(e.target.value)} required />
            </label>
            <label>
              Delivery fee ($)
              <input type="number" min="0" max="50" step="0.25" value={fee} onChange={(e) => setFee(e.target.value)} required />
            </label>
          </div>
        )}
      </fieldset>

      <p className="note">
        If fewer than the minimum are approved by the deadline, every PayPal hold is released and nobody is charged. Drops can run up to 14 days.
      </p>
      {error && <p className="error" role="alert">{error}</p>}
      <div className="form-actions">
        <button type="button" className="button button-ghost" onClick={onClose}>
          Cancel
        </button>
        <button className="button" disabled={busy || !item.trim() || !price || address.trim().length < 3}>
          {busy ? 'Creating…' : 'Create drop'}
        </button>
      </div>
    </form>
  )
}
