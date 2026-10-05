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

// Each item gets one of a few warm gradients, chosen from its name, so cards are easy to tell apart.
const TILES = [
  ['#c4512f', '#e0714f'],
  ['#c98a1f', '#e5b04e'],
  ['#2f7d5b', '#5bc08f'],
  ['#8a5a44', '#c08a6b'],
  ['#a8392f', '#ee7c74'],
]
function tileFor(name: string): string {
  let h = 0
  for (const ch of name) h = (h * 31 + ch.charCodeAt(0)) >>> 0
  const [a, b] = TILES[h % TILES.length]
  return `linear-gradient(135deg, ${a}, ${b})`
}

export function DropCard({ drop, action, showShop = true }: { drop: Drop; action?: ReactNode; showShop?: boolean }) {
  const now = useNow()
  const total = drop.quantity_total
  const approved = drop.paid_up_units
  const reserved = drop.units_by_order_status.reserved ?? 0
  const pct = (n: number) => `${Math.min(100, (n / total) * 100)}%`
  const isOpen = drop.status === 'open'
  const left = Math.max(drop.minimum_units - approved, 0)

  return (
    <article className={`card drop drop-${drop.status}`}>
      <div className="drop-tile" style={{ background: tileFor(drop.item_name) }}>
        <span className="drop-initial" aria-hidden="true">
          {drop.item_name.trim().charAt(0).toUpperCase()}
        </span>
        <span className="drop-shop">{showShop ? `by ${drop.shop_name}` : ''}</span>
        <span className={`pill pill-on-tile ${isOpen ? '' : `pill-${drop.status}`}`}>{isOpen ? timeLeft(drop.deadline, now) : STATUS_LABEL[drop.status]}</span>
      </div>

      <div className="drop-body">
        <h3>{drop.item_name}</h3>
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
          <>
            <p className="drop-progress">
              {drop.minimum_met_so_far ? (
                <strong className="good">Minimum reached · it’s happening</strong>
              ) : (
                <>
                  <strong>{left} more</strong> {left === 1 ? 'unit' : 'units'} needed to run
                </>
              )}
            </p>
            <p className="muted small">
              {drop.units_remaining} of {total} left · closes {whenText(drop.deadline)}
              {reserved > 0 ? ` · ${reserved} awaiting approval` : ''}
            </p>
          </>
        ) : (
          <p className="muted small">{CLOSED_NOTE[drop.status as Exclude<Drop['status'], 'open'>]}</p>
        )}

        {action && <footer className="drop-action">{action}</footer>}
      </div>
    </article>
  )
}
