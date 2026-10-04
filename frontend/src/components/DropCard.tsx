import type { ReactNode } from 'react'
import type { Drop } from '../api'
import { money, timeLeft, whenText } from '../format'
import { useNow } from '../hooks'

const STATUS_LABEL: Record<Drop['status'], string> = {
  open: 'Open',
  filled: 'Filled',
  failed: 'Missed minimum',
  cancelled: 'Cancelled',
}

const CLOSED_NOTE: Record<Exclude<Drop['status'], 'open'>, string> = {
  filled: 'Minimum reached. Buyers were charged.',
  failed: 'Minimum not reached. Every hold was released.',
  cancelled: 'Cancelled by the seller. Every hold was released.',
}

export function DropCard({ drop, action }: { drop: Drop; action?: ReactNode }) {
  const now = useNow()
  const total = drop.quantity_total
  const approved = drop.paid_up_units
  const reserved = drop.units_by_order_status.reserved ?? 0
  const pct = (n: number) => `${Math.min(100, (n / total) * 100)}%`
  const isOpen = drop.status === 'open'

  return (
    <article className={`card drop drop-${drop.status}`}>
      <header className="drop-head">
        <h3>{drop.item_name}</h3>
        <span className={`pill pill-${drop.status}`}>{isOpen ? timeLeft(drop.deadline, now) : STATUS_LABEL[drop.status]}</span>
      </header>

      <p className="drop-price">
        <strong>{money(drop.unit_price, drop.currency)}</strong> <span className="muted">each · max {drop.max_per_buyer} per person</span>
      </p>

      {/* Approved units (green), reserved but not yet approved (amber), and a marker at the minimum. */}
      <div className="bar" role="img" aria-label={`${approved} of ${drop.minimum_units} needed units approved, ${total} in total`}>
        <div className="bar-fill approved" style={{ width: pct(approved) }} />
        <div className="bar-fill reserved" style={{ left: pct(approved), width: pct(reserved) }} />
        <div className="bar-min" style={{ left: pct(drop.minimum_units) }} title="Minimum needed" />
      </div>

      {isOpen ? (
        <dl className="stats">
          <div>
            <dt>Approved</dt>
            <dd>
              {approved}
              <span className="muted"> / {drop.minimum_units} needed</span>
            </dd>
          </div>
          <div>
            <dt>Left</dt>
            <dd>
              {drop.units_remaining}
              <span className="muted"> of {total}</span>
            </dd>
          </div>
          <div>
            <dt>Closes</dt>
            <dd>{whenText(drop.deadline)}</dd>
          </div>
        </dl>
      ) : (
        <p className="muted small">{CLOSED_NOTE[drop.status as Exclude<Drop['status'], 'open'>]}</p>
      )}
      {isOpen && reserved > 0 && <p className="muted small">{reserved} reserved, awaiting PayPal approval</p>}

      {action && <footer className="drop-action">{action}</footer>}
    </article>
  )
}
