import { useState, type FormEvent } from 'react'
import { placeOrder, type Drop } from '../api'
import { useAuth } from '../authContext'
import { money, whenText } from '../format'
import { Modal } from './Modal'

// Reserve units, then go straight to PayPal to approve the hold. No chat needed.
export function ReserveDialog({ drop, onClose }: { drop: Drop | null; onClose: () => void }) {
  return (
    <Modal open={drop !== null} onClose={onClose} title={drop ? `Reserve ${drop.item_name}` : ''}>
      {drop && <Form drop={drop} onClose={onClose} />}
    </Modal>
  )
}

function Form({ drop, onClose }: { drop: Drop; onClose: () => void }) {
  const { me } = useAuth()
  const maxQty = Math.max(1, Math.min(drop.max_per_buyer, drop.units_remaining))
  const [quantity, setQuantity] = useState(1)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const total = Number(drop.unit_price) * quantity

  async function submit(e: FormEvent) {
    e.preventDefault()
    if (busy) return
    setBusy(true)
    setError(null)
    try {
      const placed = await placeOrder(drop.id, quantity)
      window.location.assign(placed.approval_url) // off to PayPal; PayPal returns to /orders/:id
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Could not reserve. Please try again.')
      setBusy(false)
    }
  }

  return (
    <form className="form" onSubmit={submit}>
      <div className="field-row">
        <label>
          Quantity
          <div className="stepper">
            <button type="button" onClick={() => setQuantity((q) => Math.max(1, q - 1))} disabled={quantity <= 1} aria-label="Fewer">
              −
            </button>
            <output aria-live="polite">{quantity}</output>
            <button type="button" onClick={() => setQuantity((q) => Math.min(maxQty, q + 1))} disabled={quantity >= maxQty} aria-label="More">
              +
            </button>
          </div>
        </label>
        <div className="total">
          <span className="muted small">Total (on hold)</span>
          <strong>{money(total, drop.currency)}</strong>
        </div>
      </div>

      {me?.user && (
        <p className="who muted small">
          Reserving as <strong>{me.user.name}</strong> · {me.user.email}
        </p>
      )}
      <p className="note">
        You’ll approve a <strong>hold</strong> on PayPal. You are charged only if this drop reaches {drop.minimum_units} units by{' '}
        {whenText(drop.deadline)}. Otherwise the hold is released.
      </p>
      {error && <p className="error" role="alert">{error}</p>}

      <div className="form-actions">
        <button type="button" className="button button-ghost" onClick={onClose}>
          Cancel
        </button>
        <button className="button" disabled={busy}>
          {busy ? 'Reserving…' : 'Continue to PayPal'}
        </button>
      </div>
    </form>
  )
}
