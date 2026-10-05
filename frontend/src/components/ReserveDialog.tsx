import { useEffect, useRef, useState, type FormEvent } from 'react'
import { useNavigate } from 'react-router-dom'
import { cancelOrder, confirmOrder, getConfig, placeOrder, type Drop } from '../api'
import { useAuth } from '../authContext'
import { money, whenText } from '../format'
import { Modal } from './Modal'
import { PayPalButton } from './PayPalButton'

// Reserve units, then go straight to PayPal to approve the hold. No chat needed.
export function ReserveDialog({ drop, already = 0, onClose }: { drop: Drop | null; already?: number; onClose: () => void }) {
  return (
    <Modal open={drop !== null} onClose={onClose} title={drop ? `Reserve ${drop.item_name}` : ''}>
      {drop && <Form drop={drop} already={already} onClose={onClose} />}
    </Modal>
  )
}

function Form({ drop, already, onClose }: { drop: Drop; already: number; onClose: () => void }) {
  const { me } = useAuth()
  const allowance = Math.max(drop.max_per_buyer - already, 0) // the limit is per person, across all their orders
  const maxQty = Math.max(1, Math.min(allowance, drop.units_remaining))
  const [quantity, setQuantity] = useState(1)
  const [method, setMethod] = useState<'pickup' | 'delivery'>(drop.offers_pickup ? 'pickup' : 'delivery')
  const [address, setAddress] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const navigate = useNavigate()
  const [clientId, setClientId] = useState<string | null>(null) // set when PayPal's in-page button can be used
  const orderId = useRef<number | null>(null) // the reservation made when the PayPal button is pressed
  const form = useRef({ quantity, method, address })
  useEffect(() => {
    form.current = { quantity, method, address }
  })
  useEffect(() => {
    getConfig().then((c) => setClientId(c.paypal_client_id)).catch(() => setClientId(null))
  }, [])
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

  // Pressing PayPal's button reserves the units and creates the PayPal order in one go.
  async function createForButton() {
    const f = form.current
    if (f.method === 'delivery' && f.address.trim().length < 5) throw new Error('Add the address to deliver to.')
    setError(null)
    const placed = await placeOrder(drop.id, f.quantity, f.method, f.method === 'delivery' ? f.address.trim() : undefined)
    orderId.current = placed.order_id
    return placed.paypal_order_id
  }
  async function approvedInPage() {
    if (orderId.current === null) return
    await confirmOrder(orderId.current)
    navigate(`/orders/${orderId.current}?status=authorized`)
  }
  async function cancelledInPage() {
    if (orderId.current !== null) await cancelOrder(orderId.current).catch(() => {}) // free the units right away
    orderId.current = null
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

      {already > 0 && (
        <p className="muted small">
          You already have {already} on this drop. The limit is {drop.max_per_buyer} per person.
        </p>
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

      {clientId ? (
        <>
          <PayPalButton
            clientId={clientId}
            currency={drop.currency}
            create={createForButton}
            onApproved={approvedInPage}
            onCancelled={cancelledInPage}
            onError={(m) => setError(m)}
            onUnavailable={() => setClientId(null)} // PayPal's script is blocked: fall back to the redirect button
          />
          <div className="form-actions">
            <button type="button" className="button button-ghost" onClick={onClose}>
              Cancel
            </button>
          </div>
        </>
      ) : (
        <div className="form-actions">
          <button type="button" className="button button-ghost" onClick={onClose}>
            Cancel
          </button>
          <button className="button" disabled={busy || (method === 'delivery' && address.trim().length < 5)}>
            {busy ? 'Reserving…' : 'Continue to PayPal'}
          </button>
        </div>
      )}
    </form>
  )
}
