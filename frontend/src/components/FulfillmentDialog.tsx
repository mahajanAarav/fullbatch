import { dropOrders, type Drop } from '../api'
import { money } from '../format'
import { usePolling } from '../hooks'
import { Modal } from './Modal'

// Who to hand each order to. Buyers appear by first name and last initial, and a delivery address
// shows only for orders whose payment hold is approved.
export function FulfillmentDialog({ drop, onClose }: { drop: Drop | null; onClose: () => void }) {
  return (
    <Modal open={drop !== null} onClose={onClose} title={drop ? `Orders: ${drop.item_name}` : ''}>
      {drop && <List dropId={drop.id} />}
    </Modal>
  )
}

function List({ dropId }: { dropId: number }) {
  const { data, error } = usePolling(() => dropOrders(dropId), 15_000)
  if (error && !data) return <p className="error">{error}</p>
  if (!data) return <p className="muted">Loading…</p>
  if (data.length === 0) {
    return <p className="muted">No approved orders yet. Orders appear here once a buyer approves their PayPal hold.</p>
  }
  return (
    <div className="fulfil">
      <p className="muted small">Holds aren’t charged until the drop reaches its minimum.</p>
      <ul>
        {data.map((o) => (
          <li key={o.order_id}>
            <div>
              <strong>
                {o.quantity} × {o.buyer}
              </strong>
              <span className="muted small">
                {o.fulfillment === 'delivery' ? `Deliver to ${o.delivery_address ?? 'address pending'}` : 'Pickup'}
                {' · '}
                {o.status === 'captured' ? 'charged' : 'on hold'}
              </span>
            </div>
            <span>{money(o.amount)}</span>
          </li>
        ))}
      </ul>
    </div>
  )
}
