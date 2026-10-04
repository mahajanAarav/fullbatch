import { Link } from 'react-router-dom'
import type { Drop } from '../api'
import { listOpenDrops } from '../api'
import { DropCard } from '../components/DropCard'
import { usePolling } from '../hooks'

// Shown while nothing is open, so the page never looks empty.
const EXAMPLE: Drop = {
  id: 0,
  seller_id: 0,
  item_name: 'Example: Sourdough loaves',
  unit_price: '9.00',
  currency: 'USD',
  quantity_total: 20,
  minimum_units: 10,
  max_per_buyer: 4,
  deadline: new Date(Date.now() + 2 * 86_400_000).toISOString(),
  status: 'open',
  units_taken: 12,
  units_remaining: 8,
  units_by_order_status: { authorized: 7, reserved: 5 },
  paid_up_units: 7,
  minimum_met_so_far: false,
}

const Shield = () => (
  <svg viewBox="0 0 24 24" width="22" height="22" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
    <path d="M12 3l8 3v6c0 4.5-3.2 7.8-8 9-4.8-1.2-8-4.5-8-9V6l8-3z" />
    <path d="M9 12l2 2 4-4" />
  </svg>
)
const Undo = () => (
  <svg viewBox="0 0 24 24" width="22" height="22" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
    <path d="M9 14L4 9l5-5" />
    <path d="M4 9h10a6 6 0 010 12h-3" />
  </svg>
)
const Clock = () => (
  <svg viewBox="0 0 24 24" width="22" height="22" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
    <circle cx="12" cy="12" r="9" />
    <path d="M12 7v5l3 2" />
  </svg>
)

export default function Landing() {
  const { data: open } = usePolling(listOpenDrops, 15_000)
  const featured = open && open.length > 0 ? open.slice(0, 2) : [EXAMPLE]
  const isExample = !open || open.length === 0

  return (
    <main className="wrap landing">
      <section className="hero">
        <div className="hero-copy">
          <p className="eyebrow">Preorder drops for small sellers</p>
          <h1>Make it only if enough people want it.</h1>
          <p className="lead">
            Buyers’ payments are <strong>held</strong> on PayPal, not charged. If a drop reaches its minimum by the deadline, every hold is charged. If it
            doesn’t, every hold is released and nobody pays.
          </p>
          <div className="cta-row">
            <Link to="/buy" className="button">
              Browse open drops
            </Link>
            <Link to="/sell" className="button button-ghost">
              Start selling
            </Link>
          </div>
          <ul className="trust">
            <li>
              <Shield /> Payments secured by PayPal
            </li>
            <li>
              <Undo /> No minimum, no charge
            </li>
            <li>
              <Clock /> Clear deadlines
            </li>
          </ul>
        </div>

        <div className="hero-side">
          <div className="hero-side-head">
            <h2>{isExample ? 'How a drop looks' : 'Open now'}</h2>
            {!isExample && <Link to="/buy">See all →</Link>}
          </div>
          {featured.map((d) => (
            <DropCard
              key={d.id}
              drop={d}
              action={
                isExample ? undefined : (
                  <Link to="/buy" className="button button-block">
                    Reserve
                  </Link>
                )
              }
            />
          ))}
        </div>
      </section>

      <section className="steps" aria-label="How it works">
        {[
          ['Seller opens a drop', 'Item, price, quantity, a minimum and a deadline.'],
          ['Buyers reserve', 'PayPal places a hold on each payment. No charge yet.'],
          ['Deadline arrives', 'Minimum met: every hold is charged. Missed: every hold is released.'],
          ['Plan the next drop', 'Review how it went and get a recommendation.'],
        ].map(([title, body], i) => (
          <div className="step" key={title}>
            <span className="step-n">{i + 1}</span>
            <div>
              <strong>{title}</strong>
              <p className="muted small">{body}</p>
            </div>
          </div>
        ))}
      </section>
    </main>
  )
}
