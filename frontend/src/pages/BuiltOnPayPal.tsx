import { Link } from 'react-router-dom'

// Every PayPal product fullbatch uses, where it appears in the app, and why. A map for reviewers and curious buyers.
const PARTS = [
  {
    api: 'Log in with PayPal',
    where: 'Sign in',
    what: 'Nobody makes a password. PayPal tells us who you are and whether your PayPal account is verified, and only verified accounts can open a shop.',
  },
  {
    api: 'Orders API · intent AUTHORIZE',
    where: 'Reserve, then Pay',
    what: 'Checking out creates a PayPal order that holds the money instead of taking it. The buyer approves in PayPal’s own button or page.',
  },
  {
    api: 'JavaScript SDK · Smart Payment Buttons',
    where: 'Reserve dialog',
    what: 'PayPal’s button sits inside the dialog, so a buyer approves in a PayPal popup and never leaves the page. The full-page redirect is the fallback.',
  },
  {
    api: 'Payments API · reauthorize',
    where: 'Drops that run longer than 3 days',
    what: 'PayPal guarantees a hold for 3 days. Before capturing an older hold we renew it, so long drops still get paid.',
  },
  {
    api: 'Payments API · capture',
    where: 'Deadline, drop filled',
    what: 'When the drop reaches its minimum every hold is captured. The seller is charged for nothing until that moment.',
  },
  {
    api: 'Payments API · void',
    where: 'Deadline, drop missed',
    what: 'If the drop falls short every hold is released. Nobody pays, nothing is made, and nothing needs refunding.',
  },
  {
    api: 'Payouts API',
    where: 'Seller · Payouts',
    what: 'After the captures, PayPal Payouts sends the seller their share minus a platform fee. One payout per drop, and it can never be sent twice.',
  },
  {
    api: 'Webhooks · signature verified',
    where: 'Behind the scenes',
    what: 'Order approved, capture completed or refunded, hold voided, payout succeeded or failed: PayPal’s own notices keep our records true, even if a buyer closes the tab.',
  },
]

export default function BuiltOnPayPal() {
  return (
    <main className="narrow">
      <p className="eyebrow">How it works</p>
      <h1>Built on PayPal</h1>
      <p className="lead">
        fullbatch lets neighbors pool their orders. A seller opens a drop with a minimum. PayPal holds everyone’s money until the drop
        fills, then charges everyone and pays the seller. If it doesn’t fill, PayPal releases every hold. These are the PayPal pieces
        that make that work.
      </p>
      <ol className="paypal-map">
        {PARTS.map((p) => (
          <li key={p.api} className="card">
            <strong>{p.api}</strong>
            <span className="muted small">{p.where}</span>
            <p>{p.what}</p>
          </li>
        ))}
      </ol>
      <p className="muted small">
        Everything here runs in PayPal’s sandbox, with test money. <Link to="/">Browse drops</Link>
      </p>
    </main>
  )
}
