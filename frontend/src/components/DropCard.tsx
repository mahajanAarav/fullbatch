import type { Drop } from '../api'
import { money, timeLeft, whenText } from '../format'
import { useNow } from '../hooks'

const STATUS_LABEL: Record<Drop['status'], string> = {
  open: 'Open',
  filled: 'Filled',
  failed: 'Missed minimum',
  cancelled: 'Cancelled',
}

const STATUS_TEXT: Record<Drop['status'], string> = {
  open: 'Open',
  filled: 'Filled · buyers charged',
  failed: 'Minimum not reached · all holds released',
  cancelled: 'Cancelled · all holds released',
}

export function DropCard({ drop }: { drop: Drop }) {
  const now = useNow()
  const total = drop.quantity_total
  const approved = drop.paid_up_units
  const reserved = drop.units_by_order_status.reserved ?? 0
  const pct = (n: number) => `${Math.min(100, (n / total) * 100)}%`

  return (
    <article className={`card drop drop-${drop.status}`}>
      <header className="drop-head">
        <div>
          <h3>{drop.item_name}</h3>
          <p className="muted">
            {money(drop.unit_price, drop.currency)} each · up to {drop.max_per_buyer} per person
          </p>
        </div>
        <span className={`pill pill-${drop.status}`}>{drop.status === 'open' ? timeLeft(drop.deadline, now) : STATUS_LABEL[drop.status]}</span>
      </header>

      {/* The bar shows approved units (green), reserved-but-unapproved (amber), and a marker at the minimum. */}
      <div
        className="bar"
        role="img"
        aria-label={`${approved} of ${drop.minimum_units} needed units approved, ${total} available in total`}
      >
        <div className="bar-fill approved" style={{ width: pct(approved) }} />
        <div className="bar-fill reserved" style={{ left: pct(approved), width: pct(reserved) }} />
        <div className="bar-min" style={{ left: pct(drop.minimum_units) }} title="Minimum needed" />
      </div>

      <p className="drop-line">
        {drop.status === 'open' ? (
          <>
            <strong>
              {approved} of {drop.minimum_units}
            </strong>{' '}
            needed to run · {drop.units_remaining} of {total} left
          </>
        ) : (
          STATUS_TEXT[drop.status]
        )}
      </p>
      <p className="muted small">
        {drop.status === 'open' ? 'Closes' : 'Closed'} {whenText(drop.deadline)}
        {reserved > 0 && drop.status === 'open' ? ` · ${reserved} reserved, awaiting approval` : ''}
      </p>
    </article>
  )
}
