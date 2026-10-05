import { Link, useParams, useSearchParams } from 'react-router-dom'
import { getOrder } from '../api'
import { money, timeLeft, whenText } from '../format'
import { useNow, usePolling } from '../hooks'

// Where PayPal sends a buyer back after approving (or cancelling) on PayPal.
export default function OrderStatus() {
  const { id } = useParams()
  const [query] = useSearchParams()
  const hint = query.get('status') // what the server just saw: authorized | expired | cancelled | error
  const orderId = Number(id)
  const now = useNow(1000 * 20)
  const { data: order, error } = usePolling(() => getOrder(orderId), 10_000, Number.isInteger(orderId))

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
        <Message tone="neutral" title={hint === 'cancelled' ? 'Order cancelled' : 'Waiting for approval'} order={order}>
          {hint === 'cancelled'
            ? 'You backed out on PayPal, so your reservation was released.'
            : `Your reservation is held until ${whenText(order.reserved_until)}. Finish approving on PayPal before then. If you closed the PayPal page, just reserve again.`}
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
  children,
}: {
  tone: 'good' | 'bad' | 'neutral'
  title: string
  order?: Awaited<ReturnType<typeof getOrder>>
  children: React.ReactNode
}) {
  return (
    <main className="narrow">
      <div className={`card status status-${tone}`}>
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
        <div className="status-actions">
          <Link to="/orders" className="button">
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
