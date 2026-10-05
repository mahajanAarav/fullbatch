import type { PlanReport, Recommendation } from '../api'
import { myPlan } from '../api'
import { money, whenText } from '../format'
import { usePolling } from '../hooks'
import type { DropDraft } from './CreateDropDialog'

const pct = (x: number) => `${Math.round(x * 100)}%`
const hours = (h: number) => (h >= 48 ? `${(h / 24).toFixed(1)} days` : `${Math.round(h)} hours`)

const CONFIDENCE: Record<Recommendation['confidence'], string> = {
  none: 'No history yet',
  low: 'Low confidence · 1 drop',
  medium: 'Medium confidence · 2 drops',
  high: 'High confidence · 3+ drops',
}

// How the last drop went, what to run next and why, with a one-click way to create it.
export default function Planner({ canCreate, onUse }: { canCreate: boolean; onUse: (draft: DropDraft) => void }) {
  const { data, error } = usePolling(myPlan, 30_000)
  if (error && !data) return <p className="error">{error}</p>
  if (!data) return <p className="muted">Loading your plan…</p>

  const { recommendation: rec, reports } = data
  const last = reports[0]
  const r = rec.recommended

  return (
    <div className="planner">
      {last ? <LastDrop report={last} /> : <NoHistory message={rec.message} />}

      <section className="card plan-card" aria-label="Recommended next drop">
        <header className="plan-head">
          <h2>{rec.enough_data ? 'Recommended next drop' : 'A gentle starting point'}</h2>
          <span className={`confidence confidence-${rec.confidence}`}>{CONFIDENCE[rec.confidence]}</span>
        </header>

        <dl className="plan-grid">
          {r.item_name && (
            <div className="plan-wide">
              <dt>Item</dt>
              <dd>{r.item_name}</dd>
            </div>
          )}
          {r.unit_price != null && (
            <div>
              <dt>Price</dt>
              <dd>{money(r.unit_price)}</dd>
            </div>
          )}
          <div>
            <dt>Quantity</dt>
            <dd>{r.quantity_total}</dd>
          </div>
          <div>
            <dt>Minimum to run</dt>
            <dd>{r.minimum_units}</dd>
          </div>
          <div>
            <dt>Window</dt>
            <dd>{r.duration_days} days</dd>
          </div>
          {r.deadline && (
            <div className="plan-wide">
              <dt>Suggested close</dt>
              <dd>{whenText(r.deadline)}</dd>
            </div>
          )}
        </dl>

        <h3 className="plan-why">Why</h3>
        <ul className="plan-reasons">
          {rec.reasons.map((reason) => (
            <li key={reason}>{reason}</li>
          ))}
        </ul>
        {rec.caveat && <p className="note">{rec.caveat}</p>}

        <div className="plan-actions">
          <button className="button" disabled={!canCreate} onClick={() => onUse({ ...r })}>
            Create this drop
          </button>
          <span className="muted small">{canCreate ? 'You can adjust everything before it opens.' : 'Verify your PayPal account to open drops.'}</span>
        </div>
      </section>

      {reports.length > 1 && (
        <section className="card plan-history" aria-label="Recent finished drops">
          <h2>Recent finished drops</h2>
          <table className="mini-table">
            <thead>
              <tr>
                <th>Item</th>
                <th>Result</th>
                <th>Units / minimum</th>
                <th>Sold of stock</th>
                <th>Collected</th>
              </tr>
            </thead>
            <tbody>
              {reports.map((x) => (
                <tr key={x.drop_id}>
                  <td>{x.item_name}</td>
                  <td>
                    <span className={`pill pill-${x.status}`}>{x.status === 'filled' ? 'Filled' : 'Missed'}</span>
                  </td>
                  <td>
                    {x.committed_units} / {x.minimum_units}
                  </td>
                  <td>{pct(x.sell_through)}</td>
                  <td>{money(x.revenue_collected)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </section>
      )}
    </div>
  )
}

function NoHistory({ message }: { message?: string }) {
  return (
    <section className="card plan-card" aria-label="No finished drops">
      <h2>How your last drop went</h2>
      <p className="muted">{message ?? 'Nothing to show yet. Once a drop closes, its results appear here.'}</p>
    </section>
  )
}

function LastDrop({ report: x }: { report: PlanReport }) {
  return (
    <section className="card plan-card" aria-label="Last finished drop">
      <header className="plan-head">
        <h2>How “{x.item_name}” went</h2>
        <span className={`pill pill-${x.status}`}>{x.status === 'filled' ? 'Filled · charged' : 'Missed minimum · released'}</span>
      </header>
      <dl className="plan-grid">
        <div>
          <dt>Units committed</dt>
          <dd>
            {x.committed_units} <span className="muted">of {x.quantity_total}</span>
          </dd>
        </div>
        <div>
          <dt>Minimum</dt>
          <dd>
            {x.minimum_units} <span className="muted">· {pct(x.minimum_ratio)} reached</span>
          </dd>
        </div>
        <div>
          <dt>Orders</dt>
          <dd>
            {x.committed_orders} <span className="muted">· avg {x.average_order_size} units</span>
          </dd>
        </div>
        <div>
          <dt>Time to minimum</dt>
          <dd>{x.hours_to_minimum != null ? hours(x.hours_to_minimum) : 'Never reached'}</dd>
        </div>
        <div>
          <dt>Final-day share</dt>
          <dd>{pct(x.share_in_last_day)} <span className="muted">of orders</span></dd>
        </div>
        <div>
          <dt>{x.status === 'filled' ? 'Collected' : 'Not collected'}</dt>
          <dd>{money(x.status === 'filled' ? x.revenue_collected : x.revenue_not_collected)}</dd>
        </div>
      </dl>
    </section>
  )
}
