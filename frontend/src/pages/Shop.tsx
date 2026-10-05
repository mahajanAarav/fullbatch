import { useState } from 'react'
import { Link, useNavigate, useParams } from 'react-router-dom'
import { getShop } from '../api'
import { useAuth } from '../authContext'
import { DropCard } from '../components/DropCard'
import { ReserveDialog } from '../components/ReserveDialog'
import { DropGridSkeleton } from '../components/Skeleton'
import { VerifyEmailDialog } from '../components/VerifyEmailDialog'
import { usePolling } from '../hooks'
import type { Drop } from '../api'

// A shop's public page. Everything on it comes from real activity, so it works as local trust.
export default function Shop() {
  const { id } = useParams()
  const shopId = Number(id)
  const { me } = useAuth()
  const user = me?.user ?? null
  const navigate = useNavigate()
  const { data: shop, error } = usePolling(() => getShop(shopId), 30_000, Number.isInteger(shopId))
  const [reserving, setReserving] = useState<Drop | null>(null)
  const [verifyFor, setVerifyFor] = useState<Drop | null>(null)

  const reserve = (d: Drop) => (!user ? navigate(`/signin?next=/shop/${shopId}`) : user.email_verified ? setReserving(d) : setVerifyFor(d))

  if (error && !shop) {
    return (
      <main className="narrow">
        <div className="card status status-bad">
          <h1>We couldn’t find that shop</h1>
          <Link to="/" className="button">
            Browse drops
          </Link>
        </div>
      </main>
    )
  }
  if (!shop) {
    return (
      <main className="wrap page">
        <DropGridSkeleton count={2} />
      </main>
    )
  }

  const since = new Date(shop.member_since).toLocaleDateString(undefined, { month: 'long', year: 'numeric' })

  return (
    <main className="wrap page">
      <header className="shop-head">
        <div>
          <p className="eyebrow">Local shop</p>
          <h1>
            {shop.name} {shop.verified && <span className="verify verify-yes">✓ Verified seller</span>}
          </h1>
          <p className="muted">
            {shop.area ? `${shop.area} · ` : ''}On fullbatch since {since}
          </p>
        </div>
        <dl className="shop-stats">
          <div>
            <dt>Drops filled</dt>
            <dd>
              {shop.drops_filled}
              <span className="muted"> of {shop.drops_total}</span>
            </dd>
          </div>
          <div>
            <dt>Items delivered</dt>
            <dd>{shop.units_delivered}</dd>
          </div>
          <div>
            <dt>Open now</dt>
            <dd>{shop.open_drops.length}</dd>
          </div>
        </dl>
      </header>

      <h2 className="section-title">Open drops</h2>
      {shop.open_drops.length === 0 ? (
        <div className="empty">
          <p>No open drops right now. Check back soon.</p>
          <Link to="/" className="button button-ghost">
            Browse other drops
          </Link>
        </div>
      ) : (
        <div className="grid">
          {shop.open_drops.map((d) => (
            <DropCard
              key={d.id}
              drop={d}
              showShop={false}
              action={
                <button className="button button-block" disabled={d.units_remaining === 0} onClick={() => reserve(d)}>
                  {d.units_remaining === 0 ? 'Sold out' : user ? 'Reserve' : 'Sign in to reserve'}
                </button>
              }
            />
          ))}
        </div>
      )}

      <ReserveDialog drop={reserving} onClose={() => setReserving(null)} />
      <VerifyEmailDialog open={verifyFor !== null} onClose={() => setVerifyFor(null)} onVerified={() => setReserving(verifyFor)} />
    </main>
  )
}
