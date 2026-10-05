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
  const [method, setMethod] = useState<'pickup' | 'delivery'>(drop.offers_pickup ? 'pickup' : 'delivery')
  const [address, setAddress] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const fee = method === 'delivery' ? Number(drop.delivery_fee) : 0
  const total = Number(drop.unit_price) * quantity + fee
  const miles = drop.delivery_radius_km ? (drop.delivery_radius_km / 1.609344).toFixed(1).replace(/\.0$/, '') : null

  async function submit(e: FormEvent) {
    e.preventDefault()
    if (busy) return
    setBusy(true)
    setError(null)
    try {
      const placed = await placeOrder(drop.id, quantity, method, method === 'delivery' ? address.trim() : undefined)
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

      {drop.offers_pickup && drop.offers_delivery && (
        <div className="segmented" role="radiogroup" aria-label="How do you want to get it?">
          <button type="button" role="radio" aria-checked={method === 'pickup'} className={method === 'pickup' ? 'seg seg-on' : 'seg'} onClick={() => setMethod('pickup')}>
            Pickup
          </button>
          <button type="button" role="radio" aria-checked={method === 'delivery'} className={method === 'delivery' ? 'seg seg-on' : 'seg'} onClick={() => setMethod('delivery')}>
            Delivery{fee > 0 || Number(drop.delivery_fee) > 0 ? ` +${money(drop.delivery_fee, drop.currency)}` : ' (free)'}
          </button>
        </div>
      )}
      {method === 'pickup' ? (
        <p className="where-note">
          <strong>Pickup{drop.area ? ` in ${drop.area}` : ''}.</strong> The exact address and any pickup notes are shared as soon as you approve your payment.
        </p>
      ) : (
        <label>
          Delivery address
          <input
            value={address}
            onChange={(e) => setAddress(e.target.value)}
            placeholder="Street, city and ZIP"
            autoComplete="street-address"
            required
            maxLength={300}
          />
          <span className="muted small">{miles ? `This seller delivers within ${miles} miles of ${drop.area ?? 'their location'}.` : ''}</span>
        </label>
      )}

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
        <button className="button" disabled={busy || (method === 'delivery' && address.trim().length < 5)}>
          {busy ? 'Reserving…' : 'Continue to PayPal'}
        </button>
      </div>
    </form>
  )
}
