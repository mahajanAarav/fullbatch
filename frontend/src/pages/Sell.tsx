import { useCallback, useState, type FormEvent } from 'react'
import { cancelDrop, createShop, myDrops, type Drop } from '../api'
import { AssistantDock } from '../components/AssistantDock'
import { Chat } from '../components/Chat'
import { CreateDropDialog } from '../components/CreateDropDialog'
import { DropCard } from '../components/DropCard'
import { Modal } from '../components/Modal'
import { Toast } from '../components/Toast'
import { useAuth } from '../authContext'
import { money } from '../format'
import { usePolling } from '../hooks'

export default function Sell() {
  const { me } = useAuth()
  // RequireAuth guarantees a signed-in user. They may not have opened a shop yet.
  return me?.shop ? <Workspace shop={me.shop} /> : <Onboarding />
}

function Onboarding() {
  const { me, refresh } = useAuth()
  const [name, setName] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  async function submit(e: FormEvent) {
    e.preventDefault()
    if (!name.trim() || busy) return
    setBusy(true)
    setError(null)
    try {
      await createShop(name.trim())
      await refresh()
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Could not create your shop.')
      setBusy(false)
    }
  }

  return (
    <main className="narrow">
      <form className="card form form-card" onSubmit={submit}>
        <h1>Open your shop</h1>
        <p className="muted">
          Signed in as <strong>{me?.user?.name}</strong>. Your shop name is what buyers will see.
        </p>
        {!me?.user?.paypal_verified && (
          <p className="note">
            Your PayPal account isn’t verified yet. You can set up your shop now, but you’ll need a verified account before you can open drops.
          </p>
        )}
        <label>
          Shop name
          <input value={name} onChange={(e) => setName(e.target.value)} placeholder="Maple Street Bakery" maxLength={120} autoFocus />
        </label>
        {error && <p className="error">{error}</p>}
        <button className="button" disabled={!name.trim() || busy}>
          {busy ? 'Creating…' : 'Open shop'}
        </button>
      </form>
    </main>
  )
}

// Money figures across a seller's drops.
function totals(drops: Drop[]) {
  let openDrops = 0
  let approvedUnits = 0
  let onHold = 0
  let collected = 0
  for (const d of drops) {
    const price = Number(d.unit_price)
    const u = d.units_by_order_status
    if (d.status === 'open') {
      openDrops += 1
      approvedUnits += d.paid_up_units
    }
    onHold += (u.authorized ?? 0) * price
    collected += (u.captured ?? 0) * price
  }
  return { openDrops, approvedUnits, onHold, collected }
}

function Workspace({ shop }: { shop: { id: number; name: string; verified: boolean } }) {
  const { data: drops, error, reload } = usePolling(myDrops)
  const [creating, setCreating] = useState(false)
  const [cancelling, setCancelling] = useState<Drop | null>(null)
  const [cancelError, setCancelError] = useState<string | null>(null)
  const [toast, setToast] = useState<string | null>(null)
  const clearToast = useCallback(() => setToast(null), [])
  const t = totals(drops ?? [])

  async function confirmCancel() {
    if (!cancelling) return
    try {
      await cancelDrop(cancelling.id)
      setToast(`Cancelled “${cancelling.item_name}”. Every hold was released.`)
      setCancelling(null)
      reload()
    } catch (e) {
      setCancelError(e instanceof Error ? e.message : 'Could not cancel.')
    }
  }

  return (
    <div className="shell">
      <main className="wrap main">
        <div className="page-head">
          <div>
            <h1>
              {shop.name}{' '}
              <span className={`verify ${shop.verified ? 'verify-yes' : 'verify-no'}`}>
                {shop.verified ? '✓ Verified seller' : 'Not verified'}
              </span>
            </h1>
          </div>
          <button className="button" onClick={() => setCreating(true)} disabled={!shop.verified}>
            + New drop
          </button>
        </div>

        {!shop.verified && (
          <div className="banner" role="status">
            <strong>Your PayPal account isn’t verified yet.</strong> To protect buyers, only verified sellers can open drops. Verify your account with PayPal,
            then sign out and sign back in.
          </div>
        )}

        <section className="kpis" aria-label="Summary">
          <Kpi label="Open drops" value={String(t.openDrops)} />
          <Kpi label="Units approved" value={String(t.approvedUnits)} hint="across open drops" />
          <Kpi label="On hold" value={money(t.onHold)} hint="not charged yet" />
          <Kpi label="Collected" value={money(t.collected)} hint="from filled drops" />
        </section>

        {error && <p className="error">{error}</p>}
        {drops && drops.length === 0 && (
          <div className="empty">
            <p>You haven’t opened a drop yet.</p>
            <button className="button" onClick={() => setCreating(true)} disabled={!shop.verified}>
              Create your first drop
            </button>
          </div>
        )}
        <div className="grid">
          {drops?.map((d) => (
            <DropCard
              key={d.id}
              drop={d}
              action={
                d.status === 'open' && (
                  <button className="button button-ghost button-block" onClick={() => { setCancelError(null); setCancelling(d) }}>
                    Cancel drop
                  </button>
                )
              }
            />
          ))}
        </div>
      </main>

      <AssistantDock label="Assistant">
        <Chat
          key={shop.id}
          role="seller"
          intro="I can create drops, check progress, or cancel one. You can also use the New drop button."
          suggestions={['How are my drops doing?', 'Help me set up a new drop']}
          onReply={reload}
        />
      </AssistantDock>

      <CreateDropDialog open={creating} onClose={() => setCreating(false)} onCreated={() => { reload(); setToast('Drop created. It’s open for orders.') }} />

      <Modal open={cancelling !== null} onClose={() => setCancelling(null)} title="Cancel this drop?">
        <p>
          “{cancelling?.item_name}” will close immediately and every PayPal hold will be released. Nobody is charged. This can’t be undone.
        </p>
        {cancelError && <p className="error" role="alert">{cancelError}</p>}
        <div className="form-actions">
          <button className="button button-ghost" onClick={() => setCancelling(null)}>
            Keep drop
          </button>
          <button className="button button-danger" onClick={confirmCancel}>
            Cancel drop
          </button>
        </div>
      </Modal>
      <Toast message={toast} onDone={clearToast} />
    </div>
  )
}

function Kpi({ label, value, hint }: { label: string; value: string; hint?: string }) {
  return (
    <div className="kpi card">
      <span className="kpi-label">{label}</span>
      <strong className="kpi-value">{value}</strong>
      {hint && <span className="muted small">{hint}</span>}
    </div>
  )
}
