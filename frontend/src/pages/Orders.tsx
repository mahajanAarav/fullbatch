import { useState } from 'react'
import { Link } from 'react-router-dom'
import { myOrders, payOrder, type Order } from '../api'
import { money, whenText } from '../format'
import { usePolling } from '../hooks'

const STATUS: Record<Order['status'], { label: string; hint: string }> = {
  reserved: { label: 'Waiting for PayPal approval', hint: 'Open the PayPal link to approve before the reservation expires.' },
  authorized: { label: 'On hold · not charged yet', hint: 'You’re charged only if the drop reaches its minimum.' },
  captured: { label: 'Charged', hint: 'The drop reached its minimum.' },
  voided: { label: 'Released · you were not charged', hint: 'The drop didn’t reach its minimum.' },
  expired: { label: 'Reservation expired', hint: 'It wasn’t approved in time. Nothing was charged.' },
  failed: { label: 'Payment problem', hint: 'Please contact the seller.' },
}
const ACTIVE: Order['status'][] = ['reserved', 'authorized']

export default function Orders() {
  const { data, error } = usePolling(myOrders, (list) => (list?.some((o) => o.status === 'reserved') ? 3_000 : 10_000))
  const active = data?.filter((o) => ACTIVE.includes(o.status)) ?? []
  const past = data?.filter((o) => !ACTIVE.includes(o.status)) ?? []

  return (
    <main className="wrap page">
      <div className="page-head">
        <div>
          <h1>My orders</h1>
          <p className="muted">Your reservations and what happened to each payment.</p>
        </div>
        <Link to="/" className="button button-ghost">
          Browse drops
        </Link>
      </div>

      {error && !data && <p className="error">{error}</p>}
      {!data && !error && <div className="skeleton skeleton-list" aria-label="Loading your orders" />}
      {data && data.length === 0 && (
        <div className="empty">
          <p>You haven’t reserved anything yet.</p>
          <Link to="/" className="button">
            See what’s open
          </Link>
        </div>
      )}

      {active.length > 0 && <OrderGroup title="In progress" orders={active} />}
      {past.length > 0 && <OrderGroup title="Past orders" orders={past} />}
    </main>
  )
}

function OrderGroup({ title, orders }: { title: string; orders: Order[] }) {
  const [busy, setBusy] = useState<number | null>(null)
  const [error, setError] = useState<string | null>(null)

  async function pay(id: number) {
    setBusy(id)
    setError(null)
    try {
      window.location.assign((await payOrder(id)).approval_url) // off to PayPal; it returns to the order page
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Could not open PayPal. Please try again.')
      setBusy(null)
    }
  }

  return (
    <section className="section">
      <h2>{title}</h2>
      {error && <p className="error">{error}</p>}
      <div className="list card">
        {orders.map((o) => (
          <div key={o.id} className="list-row order-row-rich">
            <Link to={`/orders/${o.id}`} className="order-link">
              <strong>
                {o.quantity} × {o.item_name}
              </strong>
              <span className={`badge badge-${o.status}`}>{STATUS[o.status].label}</span>
              <span className="muted small">
                {STATUS[o.status].hint} Drop closes {whenText(o.deadline)}.
              </span>
            </Link>
            <div className="order-side">
              <strong>{money(o.amount, o.currency)}</strong>
              {o.can_pay && (
                <button className="button button-small" onClick={() => pay(o.id)} disabled={busy !== null}>
                  {busy === o.id ? 'Opening…' : 'Pay now'}
                </button>
              )}
            </div>
          </div>
        ))}
      </div>
    </section>
  )
}
