import { useState } from 'react'
import { Link, useParams, useSearchParams } from 'react-router-dom'
import { cancelOrder, getOrder, payOrder } from '../api'
import { money, timeLeft, whenText } from '../format'
import { useNow, usePolling } from '../hooks'

// Where PayPal sends a buyer back after approving (or cancelling) on PayPal.
export default function OrderStatus() {
  const { id } = useParams()
  const [query] = useSearchParams()
  const hint = query.get('status') // what the server just saw: authorized | expired | cancelled | error
  const orderId = Number(id)
  const now = useNow(1000 * 20)
  const { data: order, error, reload } = usePolling(() => getOrder(orderId), 10_000, Number.isInteger(orderId))

  if (!id || !Number.isInteger(orderId) || hint === 'error') {
    return (
      <Message tone="bad" title="We couldn’t confirm that order">
        Something went wrong between PayPal and us. You haven’t been charged. Head back and try placing the order again.
      </Message>
    )
  }
  if (error && !order) return <Message tone="bad" title="Couldn’t load your order">{error}</Message>
  if (!order) return <main className="narrow"><p className="muted">Loading…</p></main>

  const total = money(order.amount, order.currency)
  const left = Math.max(order.minimum_units - order.paid_up_units, 0)

  switch (order.status) {
    case 'authorized':
      return (
        <Message tone="good" title="You’re in!" order={order}>
          Your {total} is <strong>on hold</strong> with PayPal. You have <strong>not</strong> been charged.
          {left > 0 ? (
            <>
              {' '}
              The drop needs <strong>{left} more {left === 1 ? 'unit' : 'units'}</strong> to run. {timeLeft(order.deadline, now)}.
            </>
          ) : (
            <> The drop has reached its minimum, so you’ll be charged when it closes {whenText(order.deadline)}.</>
          )}
          {left > 0 && <> If it falls short, the hold is released automatically.</>}
        </Message>
      )
    case 'captured':
      return (
        <Message tone="good" title="Order complete" order={order}>
          The drop reached its minimum, and {total} was charged. Thank you!
        </Message>
      )
    case 'voided':
      return (
        <Message tone="neutral" title="Hold released" order={order}>
          The drop didn’t reach its minimum, so your hold was released. You were not charged.
        </Message>
      )
    case 'reserved':
      return (
        <Message
          tone="neutral"
          title={hint === 'cancelled' && !order.can_pay ? 'Order cancelled' : order.can_pay ? 'Finish your payment' : 'Reservation ended'}
          order={order}
          actions={order.can_pay ? <PayActions order={order} now={now} onChanged={reload} /> : undefined}
        >
          {order.can_pay
            ? `This is your order, held for you until ${whenText(order.reserved_until)} (${timeLeft(order.reserved_until, now)}). Approve it on PayPal to lock it in. PayPal only places a hold, and you’re charged only if the drop reaches its minimum.`
            : 'This reservation is no longer held, so nothing was charged. You can reserve again from the drop.'}
        </Message>
      )
    case 'expired':
      return (
        <Message tone="bad" title={hint === 'cancelled' ? 'Order cancelled' : 'Reservation expired'} order={order}>
          {hint === 'cancelled'
            ? 'You backed out on PayPal, so your reservation was released. Nothing was charged.'
            : 'You didn’t approve in time, so the units went back to the pool. Nothing was charged. Place the order again to try once more.'}
        </Message>
      )
    default:
      return (
        <Message tone="bad" title="There was a payment problem" order={order}>
          We couldn’t finish this payment. Please contact the seller. You haven’t been charged more than once.
        </Message>
      )
  }
}

function Message({
  tone,
  title,
  order,
  actions,
  children,
}: {
  tone: 'good' | 'bad' | 'neutral'
  title: string
  order?: Awaited<ReturnType<typeof getOrder>>
  actions?: React.ReactNode
  children: React.ReactNode
}) {
  return (
    <main className="narrow">
      <div className={`card status status-${tone}`}>
        {order && <p className="eyebrow">Your order #{order.id}</p>}
        <h1>{title}</h1>
        <p>{children}</p>
        {order && <Where order={order} />}
        {order && (
          <dl className="facts">
            <dt>Item</dt>
            <dd>
              {order.quantity} × {order.item_name}
            </dd>
            <dt>Total</dt>
            <dd>{money(order.amount, order.currency)}</dd>
            <dt>Drop closes</dt>
            <dd>{whenText(order.deadline)}</dd>
          </dl>
        )}
        {order && <PayPalTrail order={order} />}
        {actions}
        <div className="status-actions">
          <Link to="/orders" className={actions ? 'button button-ghost' : 'button'}>
            View my orders
          </Link>
          <Link to="/" className="button button-ghost">
            Browse more drops
          </Link>
        </div>
      </div>
    </main>
  )
}


// The PayPal side of the order: where the money is now, and every PayPal step so far.
function PayPalTrail({ order }: { order: Awaited<ReturnType<typeof getOrder>> }) {
  const holding = order.status === 'authorized' && order.paypal.authorization_id
  return (
    <section className="trail" aria-label="PayPal activity">
      <h2 className="trail-title">PayPal activity</h2>
      {holding && (
        <p className="muted small">
          Hold <code>{order.paypal.authorization_id}</code>
          {order.paypal.hold_expires && <> · PayPal keeps it until {whenText(order.paypal.hold_expires)}; we renew it if the drop runs long</>}
        </p>
      )}
      {order.paypal.capture_id && (
        <p className="muted small">
          Charge <code>{order.paypal.capture_id}</code>
        </p>
      )}
      <ol className="trail-list">
        {order.timeline.map((s, i) => (
          <li key={i}>
            <span>{s.label}</span>
            {s.via && <span className="trail-via">{s.via}</span>}
            <time className="muted small" dateTime={s.at}>{whenText(s.at)}</time>
          </li>
        ))}
      </ol>
    </section>
  )
}


// Where to collect it, or where it is going. The exact pickup address appears only after the hold is approved.
function Where({ order }: { order: Awaited<ReturnType<typeof getOrder>> }) {
  const approved = order.status === 'authorized' || order.status === 'captured'
  if (order.fulfillment === 'delivery') {
    return approved || order.status === 'reserved' ? (
      <div className="where-card">
        <strong>Delivering to</strong>
        <p>{order.delivery_address}</p>
        {Number(order.delivery_fee) > 0 && <span className="muted small">Includes a {money(order.delivery_fee, order.currency)} delivery fee.</span>}
      </div>
    ) : null
  }
  if (approved && order.pickup_address) {
    return (
      <div className="where-card">
        <strong>Pickup address</strong>
        <p>{order.pickup_address}</p>
        {order.pickup_notes && <span className="muted small">{order.pickup_notes}</span>}
      </div>
    )
  }
  if (order.status === 'reserved') {
    return (
      <div className="where-card where-hidden">
        <strong>Pickup{order.area ? ` in ${order.area}` : ''}</strong>
        <span className="muted small">The exact address and pickup notes are shared as soon as you approve your payment.</span>
      </div>
    )
  }
  return null
}


// Resume paying for a reservation that is still held, or give it up.
function PayActions({ order, now, onChanged }: { order: Awaited<ReturnType<typeof getOrder>>; now: number; onChanged: () => void }) {
  const [busy, setBusy] = useState<'pay' | 'cancel' | null>(null)
  const [error, setError] = useState<string | null>(null)
  const left = new Date(order.reserved_until).getTime() - now

  async function pay() {
    setBusy('pay')
    setError(null)
    try {
      window.location.assign((await payOrder(order.id)).approval_url) // off to PayPal; it returns here
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Could not open PayPal. Please try again.')
      setBusy(null)
    }
  }
  async function cancel() {
    if (!window.confirm('Cancel this reservation? The units go back to other buyers.')) return
    setBusy('cancel')
    setError(null)
    try {
      await cancelOrder(order.id)
      onChanged()
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Could not cancel.')
    } finally {
      setBusy(null)
    }
  }

  return (
    <div className="pay-actions">
      <button className="button button-paypal" onClick={pay} disabled={busy !== null || left <= 0}>
        {busy === 'pay' ? 'Opening PayPal…' : `Complete payment on PayPal · ${money(order.amount, order.currency)}`}
      </button>
      <button className="link-button small" onClick={cancel} disabled={busy !== null}>
        {busy === 'cancel' ? 'Cancelling…' : 'Cancel reservation'}
      </button>
      {error && <p className="error" role="alert">{error}</p>}
    </div>
  )
}
