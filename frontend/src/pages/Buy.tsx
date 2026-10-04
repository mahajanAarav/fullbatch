import { useCallback, useEffect, useRef, useState } from 'react'
import { Link } from 'react-router-dom'
import { buyerOrders, listOpenDrops, type Drop, type Order } from '../api'
import { AssistantDock } from '../components/AssistantDock'
import { Chat } from '../components/Chat'
import { DropCard } from '../components/DropCard'
import { ReserveDialog } from '../components/ReserveDialog'
import { Toast } from '../components/Toast'
import { money } from '../format'
import { usePolling } from '../hooks'
import { getBuyerSessionId } from '../identity'

const ORDER_STATUS_TEXT: Record<Order['status'], string> = {
  reserved: 'Waiting for PayPal approval',
  authorized: 'On hold · not charged yet',
  captured: 'Charged',
  voided: 'Released · not charged',
  expired: 'Reservation expired',
  failed: 'Payment problem',
}

export default function Buy() {
  const sessionId = getBuyerSessionId()
  const [reserving, setReserving] = useState<Drop | null>(null)
  const [toast, setToast] = useState<string | null>(null)
  const [chatSync, setChatSync] = useState(0)
  const clearToast = useCallback(() => setToast(null), [])

  const drops = usePolling(listOpenDrops)
  // While a payment is waiting on PayPal, check every few seconds so the page updates by itself.
  const orders = usePolling(
    () => buyerOrders(sessionId),
    (list) => (list?.some((o) => o.status === 'reserved') ? 3_000 : 10_000),
  )
  const seen = useRef<Map<number, Order['status']>>(new Map())

  useEffect(() => {
    const list = orders.data
    if (!list) return
    for (const o of list) {
      const before = seen.current.get(o.id)
      if (before === 'reserved' && o.status === 'authorized') {
        setToast(`Payment on hold: ${money(o.amount, o.currency)} for ${o.quantity} × ${o.item_name}. You haven’t been charged.`)
        setChatSync((n) => n + 1) // pull in the confirmation the server added to the chat
        drops.reload()
      }
      seen.current.set(o.id, o.status)
    }
  }, [orders.data]) // eslint-disable-line react-hooks/exhaustive-deps

  const refresh = () => {
    drops.reload()
    orders.reload()
  }

  return (
    <div className="shell">
      <main className="wrap main">
        <div className="page-head">
          <div>
            <h1>Open drops</h1>
            <p className="muted">Reserve what you want. You’re only charged if the drop reaches its minimum.</p>
          </div>
        </div>

        {drops.error && <p className="error">{drops.error}</p>}
        {drops.data && drops.data.length === 0 && <p className="empty">Nothing is open right now. Check back soon.</p>}
        <div className="grid">
          {drops.data?.map((d) => (
            <DropCard
              key={d.id}
              drop={d}
              action={
                <button className="button button-block" disabled={d.units_remaining === 0} onClick={() => setReserving(d)}>
                  {d.units_remaining === 0 ? 'Sold out' : 'Reserve'}
                </button>
              }
            />
          ))}
        </div>

        {orders.data && orders.data.length > 0 && (
          <section className="section">
            <h2>Your orders</h2>
            <div className="list card">
              {orders.data.map((o) => (
                <Link key={o.id} to={`/orders/${o.id}`} className="list-row">
                  <div>
                    <strong>
                      {o.quantity} × {o.item_name}
                    </strong>
                    <span className={`badge badge-${o.status}`}>{ORDER_STATUS_TEXT[o.status]}</span>
                  </div>
                  <strong>{money(o.amount, o.currency)}</strong>
                </Link>
              ))}
            </div>
          </section>
        )}
      </main>

      <AssistantDock label="Assistant">
        <Chat
          key={sessionId}
          role="buyer"
          sessionId={sessionId}
          intro="Ask me anything about the drops, or have me place an order. You can also just use the Reserve buttons."
          suggestions={['What’s available?', 'Will I be charged right away?']}
          onReply={refresh}
          syncKey={chatSync}
        />
      </AssistantDock>

      <ReserveDialog drop={reserving} onClose={() => setReserving(null)} />
      <Toast message={toast} onDone={clearToast} />
    </div>
  )
}
