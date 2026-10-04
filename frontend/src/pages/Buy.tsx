import { Link } from 'react-router-dom'
import { buyerOrders, listOpenDrops, type Order } from '../api'
import { Chat } from '../components/Chat'
import { DropCard } from '../components/DropCard'
import { money } from '../format'
import { usePolling } from '../hooks'
import { getBuyerSessionId } from '../identity'

const ORDER_STATUS_TEXT: Record<Order['status'], string> = {
  reserved: 'Waiting for your PayPal approval',
  authorized: 'Approved · on hold, not charged yet',
  captured: 'Charged',
  voided: 'Released · you were not charged',
  expired: 'Reservation expired',
  failed: 'Payment problem',
}

export default function Buy() {
  const sessionId = getBuyerSessionId()
  const drops = usePolling(listOpenDrops)
  const orders = usePolling(() => buyerOrders(sessionId))

  const refresh = () => {
    drops.reload()
    orders.reload()
  }

  return (
    <main className="workspace">
      <div className="workspace-head">
        <div>
          <p className="eyebrow">Buyer</p>
          <h1>What’s dropping</h1>
        </div>
      </div>

      <div className="columns">
        <Chat
          key={sessionId}
          role="buyer"
          sessionId={sessionId}
          intro="Ask what’s available, pick a drop, and I’ll reserve your order and send you a PayPal link to approve."
          suggestions={['What can I buy right now?', 'Will I be charged right away?']}
          onReply={refresh}
        />

        <aside className="panel">
          <h2>Open drops</h2>
          {drops.error && <p className="error">{drops.error}</p>}
          {drops.data && drops.data.length === 0 && <p className="muted empty">Nothing is open right now. Check back soon.</p>}
          {drops.data?.map((d) => <DropCard key={d.id} drop={d} />)}

          {orders.data && orders.data.length > 0 && (
            <>
              <h2 className="panel-gap">Your orders</h2>
              {orders.data.map((o) => (
                <Link key={o.id} to={`/orders/${o.id}`} className="card order-row">
                  <div>
                    <strong>
                      {o.quantity} × {o.item_name}
                    </strong>
                    <span className="muted small">{ORDER_STATUS_TEXT[o.status]}</span>
                  </div>
                  <span>{money(o.amount, o.currency)}</span>
                </Link>
              ))}
            </>
          )}
        </aside>
      </div>
    </main>
  )
}
