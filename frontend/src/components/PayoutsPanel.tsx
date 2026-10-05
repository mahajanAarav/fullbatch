import { myPayouts, type PayoutRow } from '../api'
import { money } from '../format'
import { usePolling } from '../hooks'

const WORDS: Record<PayoutRow['status'], string> = {
  pending: 'Sending',
  success: 'Paid',
  unclaimed: 'Waiting for you to claim it',
  denied: 'PayPal declined it',
  unavailable: 'Will retry',
  skipped: 'No PayPal account to pay',
}

// What fullbatch sent the seller through PayPal Payouts after each filled drop, and the fee kept.
export function PayoutsPanel() {
  const { data } = usePolling(myPayouts, 15_000)
  if (!data || data.payouts.length === 0) return null
  return (
    <section className="payouts card" aria-label="Payouts">
      <h2>Payouts</h2>
      <p className="muted small">
        When a drop fills, PayPal Payouts sends you the money minus a {data.fee_percent}% fullbatch fee.
      </p>
      <table>
        <thead>
          <tr>
            <th>Drop</th>
            <th className="num">Collected</th>
            <th className="num">Fee</th>
            <th className="num">You get</th>
            <th>Status</th>
          </tr>
        </thead>
        <tbody>
          {data.payouts.map((p) => (
            <tr key={p.drop_id}>
              <td>
                {p.item_name}
                {p.batch_id && <div className="payout-batch muted">{p.batch_id}</div>}
              </td>
              <td className="num">{money(p.gross, p.currency)}</td>
              <td className="num">{money(p.fee, p.currency)}</td>
              <td className="num"><strong>{money(p.net, p.currency)}</strong></td>
              <td>
                <span className={`pill-payout pill-payout-${p.status}`} title={p.detail ?? undefined}>{WORDS[p.status]}</span>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </section>
  )
}
