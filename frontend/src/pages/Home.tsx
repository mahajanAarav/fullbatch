import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import { listOpenDrops, myOrders, payOrder, type Drop, type Order } from '../api'
import { AssistantDock } from '../components/AssistantDock'
import { Chat } from '../components/Chat'
import { DropCard } from '../components/DropCard'
import { DropsMap } from '../components/DropsMap'
import { ReserveDialog } from '../components/ReserveDialog'
import { VerifyEmailDialog } from '../components/VerifyEmailDialog'
import { DropGridSkeleton } from '../components/Skeleton'
import { Toast } from '../components/Toast'
import { useAuth } from '../authContext'
import { money, timeLeft } from '../format'
import { useNow, usePolling } from '../hooks'
import { currentPosition, distanceMiles, KM_PER_MILE, lookupZip, useBuyerLocation } from '../location'

type Sort = 'ending' | 'nearest' | 'closest' | 'available'
const SORTS: Record<Sort, string> = {
  nearest: 'Nearest',
  ending: 'Ending soon',
  closest: 'Closest to its minimum',
  available: 'Most available',
}
const TIP_KEY = 'fullbatch.tipSeen'

const readFlag = (key: string) => {
  try {
    return localStorage.getItem(key) === '1'
  } catch {
    return false
  }
}

export default function Home() {
  const { me } = useAuth()
  const user = me?.user ?? null
  const navigate = useNavigate()
  const now = useNow()
  const [reserving, setReserving] = useState<Drop | null>(null)
  const [verifyFor, setVerifyFor] = useState<Drop | null>(null) // the drop to reserve once their email is confirmed
  const [toast, setToast] = useState<string | null>(null)
  const [chatSync, setChatSync] = useState(0)
  const [search, setSearch] = useState('')
  const [sort, setSort] = useState<Sort>('ending')
  const [location, setLocation] = useBuyerLocation()
  const [within, setWithin] = useState<number>(0) // miles; 0 means any distance
  const [view, setView] = useState<'list' | 'map'>('list')
  const [area, setArea] = useState<string | null>(null) // a neighborhood to focus on
  const [tipSeen, setTipSeen] = useState(() => readFlag(TIP_KEY))
  const clearToast = useCallback(() => setToast(null), [])

  const drops = usePolling(listOpenDrops)
  // While a payment is waiting on PayPal, check every few seconds so the page updates by itself.
  const orders = usePolling(
    myOrders,
    (list) => (list?.some((o) => o.status === 'reserved') ? 3_000 : 10_000),
    user !== null,
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

  const distanceOf = useCallback(
    (d: Drop) => (location && d.lat !== null && d.lng !== null ? distanceMiles(location, { lat: d.lat, lng: d.lng }) : null),
    [location],
  )

  const shown = useMemo(() => {
    const q = search.trim().toLowerCase()
    const list = (drops.data ?? []).filter((d) => {
      if (q && !d.item_name.toLowerCase().includes(q) && !d.shop_name.toLowerCase().includes(q) && !(d.area ?? '').toLowerCase().includes(q)) return false
      if (area && d.area !== area) return false
      const away = distanceOf(d)
      return !(within > 0 && away !== null && away > within)
    })
    const far = Number.POSITIVE_INFINITY
    const by: Record<Sort, (a: Drop, b: Drop) => number> = {
      nearest: (a, b) => (distanceOf(a) ?? far) - (distanceOf(b) ?? far),
      ending: (a, b) => new Date(a.deadline).getTime() - new Date(b.deadline).getTime(),
      closest: (a, b) => b.paid_up_units / b.minimum_units - a.paid_up_units / a.minimum_units,
      available: (a, b) => b.units_remaining - a.units_remaining,
    }
    return [...list].sort(by[location || sort !== 'nearest' ? sort : 'ending'])
  }, [drops.data, search, sort, within, location, distanceOf, area])

  // Neighborhoods that have open drops, with how many.
  const areas = useMemo(() => {
    const counts = new Map<string, number>()
    for (const d of drops.data ?? []) if (d.area) counts.set(d.area, (counts.get(d.area) ?? 0) + 1)
    return [...counts.entries()].sort((a, b) => b[1] - a[1])
  }, [drops.data])

  // What this person already has on each drop: their units, and any order they could still finish paying for.
  const mine = useMemo(() => {
    const byDrop = new Map<number, { units: number; orders: Order[] }>()
    for (const o of orders.data ?? []) {
      if (!['reserved', 'authorized', 'captured'].includes(o.status)) continue
      const entry = byDrop.get(o.drop_id) ?? { units: 0, orders: [] }
      entry.units += o.quantity
      entry.orders.push(o)
      byDrop.set(o.drop_id, entry)
    }
    return byDrop
  }, [orders.data])

  const refresh = () => {
    drops.reload()
    orders.reload()
  }
  const reserve = (d: Drop) => (!user ? navigate('/signin?next=/') : user.email_verified ? setReserving(d) : setVerifyFor(d))
  const dismissTip = () => {
    setTipSeen(true)
    try {
      localStorage.setItem(TIP_KEY, '1')
    } catch {
      /* not essential */
    }
  }

  const soonest = drops.data?.[0]

  return (
    <div className="shell">
      <main className="wrap main">
        <div className="page-head">
          <div>
            <h1>Open drops</h1>
            {soonest && <p className="muted small">Next to close: {soonest.item_name} · {timeLeft(soonest.deadline, now)}</p>}
          </div>
          <div className="toolbar">
            <input
              className="search"
              type="search"
              placeholder="Search items or shops"
              value={search}
              onChange={(e) => setSearch(e.target.value)}
              aria-label="Search drops"
            />
            <div className="segmented view-toggle" role="radiogroup" aria-label="View">
              <button type="button" role="radio" aria-checked={view === 'list'} className={view === 'list' ? 'seg seg-on' : 'seg'} onClick={() => setView('list')}>
                List
              </button>
              <button type="button" role="radio" aria-checked={view === 'map'} className={view === 'map' ? 'seg seg-on' : 'seg'} onClick={() => setView('map')}>
                Map
              </button>
            </div>
            <select className="select" value={sort} onChange={(e) => setSort(e.target.value as Sort)} aria-label="Sort drops">
              {Object.entries(SORTS)
                .filter(([value]) => location || value !== 'nearest')
                .map(([value, label]) => (
                  <option key={value} value={value}>
                    {label}
                  </option>
                ))}
            </select>
          </div>
        </div>

        <LocationBar
          location={location}
          within={within}
          onWithin={setWithin}
          onChange={(loc) => {
            setLocation(loc)
            if (loc) setSort('nearest')
            else {
              setWithin(0)
              if (sort === 'nearest') setSort('ending')
            }
          }}
        />

        {areas.length > 0 && (
          <div className="chips-row" role="group" aria-label="Neighborhoods">
            <button className={area === null ? 'chip chip-on' : 'chip'} onClick={() => setArea(null)}>
              All areas
            </button>
            {areas.map(([name, count]) => (
              <button key={name} className={area === name ? 'chip chip-on' : 'chip'} onClick={() => setArea(area === name ? null : name)}>
                {name} · {count}
              </button>
            ))}
          </div>
        )}

        {!tipSeen && (
          <div className="tip" role="note">
            <span>
              <strong>How paying works:</strong> PayPal only <em>holds</em> your payment. You’re charged only if the drop reaches its minimum by the deadline;
              otherwise the hold is released.
            </span>
            <button className="icon-button" onClick={dismissTip} aria-label="Dismiss tip">
              ×
            </button>
          </div>
        )}

        {drops.error && !drops.data && <p className="error">{drops.error}</p>}
        {!drops.data && !drops.error && <DropGridSkeleton />}
        {drops.data && drops.data.length === 0 && (
          <div className="empty">
            <p>Nothing is open right now.</p>
            <Link to="/sell" className="button">
              Open the first drop
            </Link>
          </div>
        )}
        {drops.data && drops.data.length > 0 && shown.length === 0 && (
          <div className="empty">
            <p>{search ? `No drops match “${search}”.` : `No open drops within ${within} miles.`}</p>
            <button className="button button-ghost" onClick={() => { setSearch(''); setWithin(0) }}>
              Show everything
            </button>
          </div>
        )}
        {view === 'map' && drops.data && (
          <>
            <DropsMap drops={shown} location={location} onReserve={reserve} onOpenShop={(id) => navigate(`/shop/${id}`)} />
            {shown.some((d) => d.lat === null) && (
              <p className="muted small map-note">
                {shown.filter((d) => d.lat === null).length} drop(s) have no location, so they appear in the list only.
              </p>
            )}
          </>
        )}
        <div className="grid" hidden={view === 'map'}>
          {shown.map((d) => (
            <DropCard
              key={d.id}
              drop={d}
              distance={distanceOf(d)}
              deliversToYou={
                location && d.offers_delivery && d.delivery_radius_km !== null && distanceOf(d) !== null ? distanceOf(d)! * KM_PER_MILE <= d.delivery_radius_km : null
              }
              yourOrder={mine.has(d.id) ? <YourOrder entry={mine.get(d.id)!} /> : undefined}
              action={
                <button
                  className="button button-block"
                  disabled={d.units_remaining === 0 || (mine.get(d.id)?.units ?? 0) >= d.max_per_buyer}
                  onClick={() => reserve(d)}
                >
                  {d.units_remaining === 0
                    ? 'Sold out'
                    : (mine.get(d.id)?.units ?? 0) >= d.max_per_buyer
                      ? 'You’ve reached the limit'
                      : !user
                        ? 'Sign in to reserve'
                        : mine.has(d.id)
                          ? 'Reserve more'
                          : 'Reserve'}
                </button>
              }
            />
          ))}
        </div>

        <section className="how" aria-label="How it works">
          <h2>How it works</h2>
          <ol>
            <li>
              <span>1</span>
              <div>
                <strong>Reserve</strong>
                <p className="muted small">Pick a drop and a quantity.</p>
              </div>
            </li>
            <li>
              <span>2</span>
              <div>
                <strong>Approve a hold</strong>
                <p className="muted small">PayPal holds the amount. You aren’t charged yet.</p>
              </div>
            </li>
            <li>
              <span>3</span>
              <div>
                <strong>Deadline</strong>
                <p className="muted small">Minimum met: you’re charged. Missed: the hold is released.</p>
              </div>
            </li>
          </ol>
        </section>

        <section className="cta-band">
          <div>
            <h2>Sell your own drop</h2>
            <p className="muted">Open a preorder, set a minimum, and only bake when enough people want it.</p>
          </div>
          <Link to="/sell" className="button">
            Start selling
          </Link>
        </section>
      </main>

      <AssistantDock label="Assistant">
        {user ? (
          <Chat
            key={user.id}
            role="buyer"
            intro="Ask me anything about the drops, or have me place an order. You can also just use the Reserve buttons."
            suggestions={['What’s available?', 'Will I be charged right away?']}
            onReply={refresh}
            syncKey={chatSync}
          />
        ) : (
          <div className="chat-empty dock-signin">
            <p>Sign in to chat with the assistant and keep track of your orders.</p>
            <Link to="/signin?next=/" className="button">
              Sign in
            </Link>
          </div>
        )}
      </AssistantDock>

      <ReserveDialog drop={reserving} already={reserving ? (mine.get(reserving.id)?.units ?? 0) : 0} onClose={() => setReserving(null)} />
      <VerifyEmailDialog open={verifyFor !== null} onClose={() => setVerifyFor(null)} onVerified={() => setReserving(verifyFor)} />
      <Toast message={toast} onDone={clearToast} />
    </div>
  )
}


// "Near me": a ZIP code or the browser's location, used only here, in this browser, to sort and filter by distance.
function LocationBar({
  location,
  within,
  onWithin,
  onChange,
}: {
  location: { label: string } | null
  within: number
  onWithin: (miles: number) => void
  onChange: (loc: Awaited<ReturnType<typeof lookupZip>> | null) => void
}) {
  const [zip, setZip] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  async function run(get: () => Promise<Awaited<ReturnType<typeof lookupZip>>>) {
    setBusy(true)
    setError(null)
    try {
      onChange(await get())
      setZip('')
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Could not set your location.')
    } finally {
      setBusy(false)
    }
  }

  if (location) {
    return (
      <div className="locbar">
        <span>
          Showing drops near <strong>{location.label}</strong>
        </span>
        <select className="select" value={within} onChange={(e) => onWithin(Number(e.target.value))} aria-label="Distance">
          <option value={0}>Any distance</option>
          <option value={5}>Within 5 miles</option>
          <option value={10}>Within 10 miles</option>
          <option value={25}>Within 25 miles</option>
        </select>
        <button className="link-button small" onClick={() => onChange(null)}>
          Clear
        </button>
      </div>
    )
  }
  return (
    <form
      className="locbar"
      onSubmit={(e) => {
        e.preventDefault()
        run(() => lookupZip(zip))
      }}
    >
      <span className="muted">Find drops near you</span>
      <input className="search zip" value={zip} onChange={(e) => setZip(e.target.value.replace(/\D/g, '').slice(0, 5))} placeholder="ZIP code" inputMode="numeric" aria-label="ZIP code" />
      <button className="button button-ghost button-small" disabled={busy || zip.length !== 5}>
        {busy ? 'Looking…' : 'Go'}
      </button>
      <button type="button" className="link-button small" onClick={() => run(currentPosition)} disabled={busy}>
        Use my location
      </button>
      {error && <span className="error small">{error}</span>}
    </form>
  )
}


// "This is your order": shown on a drop you already have units in, with a way to finish paying if you haven't.
function YourOrder({ entry }: { entry: { units: number; orders: Order[] } }) {
  const unpaid = entry.orders.find((o) => o.can_pay)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const state = entry.orders.every((o) => o.status === 'captured') ? 'charged' : unpaid ? 'payment not finished' : 'on hold, not charged yet'

  async function pay() {
    if (!unpaid) return
    setBusy(true)
    setError(null)
    try {
      window.location.assign((await payOrder(unpaid.id)).approval_url)
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Could not open PayPal.')
      setBusy(false)
    }
  }

  return (
    <>
      <div className="your-order-row">
        <span>
          <strong>Your order:</strong> {entry.units} {entry.units === 1 ? 'unit' : 'units'} · {state}
        </span>
        <Link to={`/orders/${(unpaid ?? entry.orders[0]).id}`}>View</Link>
      </div>
      {unpaid && (
        <button className="button button-paypal button-block" onClick={pay} disabled={busy}>
          {busy ? 'Opening PayPal…' : 'Complete payment'}
        </button>
      )}
      {error && <span className="error small">{error}</span>}
    </>
  )
}
