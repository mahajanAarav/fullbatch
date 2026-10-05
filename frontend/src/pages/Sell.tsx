import { lazy, Suspense, useCallback, useEffect, useState, type FormEvent } from 'react'
import { cancelDrop, createShop, myDrops, type Drop } from '../api'
import { AssistantDock } from '../components/AssistantDock'
import { Chat } from '../components/Chat'
import { CreateDropDialog, type DropDraft } from '../components/CreateDropDialog'
import { DropCard } from '../components/DropCard'
import { Modal } from '../components/Modal'
import { Toast } from '../components/Toast'
import { VerifyEmailDialog } from '../components/VerifyEmailDialog'
import { useAuth } from '../authContext'
import { money } from '../format'
import { usePolling } from '../hooks'
import { PREPARE_DROP_EVENT } from '../studio/plannerAgent'

// AG Studio is a large library, so it only loads when a seller opens the Dashboard tab.
const Dashboard = lazy(() => import('../components/Dashboard'))
const Planner = lazy(() => import('../components/Planner'))

export default function Sell() {
  const { me } = useAuth()
  // RequireAuth guarantees a signed-in user. They may not have opened a shop yet.
  return me?.shop ? <Workspace shop={me.shop} /> : <Onboarding />
}

function Onboarding() {
  const { me, refresh } = useAuth()
  const [verifying, setVerifying] = useState(false)
  const needsEmail = me?.user?.email_verified === false
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
        {needsEmail && (
          <div className="note">
            <strong>First, confirm your email.</strong> We’ll send a short code to {me?.user?.email} so buyers and PayPal can always reach you.{' '}
            <button type="button" className="link-button" onClick={() => setVerifying(true)}>
              Verify email
            </button>
          </div>
        )}
        <label>
          Shop name
          <input value={name} onChange={(e) => setName(e.target.value)} placeholder="Maple Street Bakery" maxLength={120} autoFocus={!needsEmail} />
        </label>
        {error && <p className="error">{error}</p>}
        <button className="button" disabled={!name.trim() || busy || needsEmail}>
          {busy ? 'Creating…' : 'Open shop'}
        </button>
      </form>
      <VerifyEmailDialog open={verifying} onClose={() => setVerifying(false)} />
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
  const [tab, setTab] = useState<'drops' | 'dashboard' | 'planner'>('drops')
  const [creating, setCreating] = useState(false)
  const [draft, setDraft] = useState<DropDraft | undefined>(undefined) // set when the planner pre-fills the form
  const [cancelling, setCancelling] = useState<Drop | null>(null)
  const [cancelError, setCancelError] = useState<string | null>(null)
  const [toast, setToast] = useState<string | null>(null)
  const clearToast = useCallback(() => setToast(null), [])
  const t = totals(drops ?? [])

  // The dashboard's Drop planner agent can ask for the New drop form to open, pre-filled. It never creates the drop.
  useEffect(() => {
    const open = (e: Event) => {
      setDraft((e as CustomEvent<DropDraft>).detail)
      setCreating(true)
    }
    window.addEventListener(PREPARE_DROP_EVENT, open)
    return () => window.removeEventListener(PREPARE_DROP_EVENT, open)
  }, [])

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
    <div className={tab === 'dashboard' ? 'shell shell-wide' : 'shell'}>
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
          <button className="button" onClick={() => { setDraft(undefined); setCreating(true) }} disabled={!shop.verified}>
            + New drop
          </button>
        </div>

        <SetupChecklist verified={shop.verified} hasDrop={(drops?.length ?? 0) > 0} />

        <div className="tabs" role="tablist" aria-label="Seller views">
          <button role="tab" aria-selected={tab === 'drops'} className={tab === 'drops' ? 'tab tab-on' : 'tab'} onClick={() => setTab('drops')}>
            Drops
          </button>
          <button role="tab" aria-selected={tab === 'planner'} className={tab === 'planner' ? 'tab tab-on' : 'tab'} onClick={() => setTab('planner')}>
            Planner
          </button>
          <button role="tab" aria-selected={tab === 'dashboard'} className={tab === 'dashboard' ? 'tab tab-on' : 'tab'} onClick={() => setTab('dashboard')}>
            Dashboard
          </button>
        </div>

        {tab === 'dashboard' ? (
          <Suspense fallback={<p className="muted">Loading your dashboard…</p>}>
            <Dashboard />
          </Suspense>
        ) : tab === 'planner' ? (
          <Suspense fallback={<p className="muted">Loading your plan…</p>}>
            <Planner canCreate={shop.verified} onUse={(d) => { setDraft(d); setCreating(true) }} />
          </Suspense>
        ) : (
          <>
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
          </>
        )}
      </main>

      <AssistantDock key={tab === 'dashboard' ? 'overlay' : 'docked'} label="Assistant" overlay={tab === 'dashboard'}>
        <Chat
          key={shop.id}
          role="seller"
          intro="I can create drops, check progress, or cancel one. You can also use the New drop button."
          suggestions={['How did my last drop go?', 'What should I run next?']}
          onReply={reload}
        />
      </AssistantDock>

      <CreateDropDialog open={creating} draft={draft} onClose={() => setCreating(false)} onCreated={() => { reload(); setTab('drops'); setToast('Drop created. It’s open for orders.') }} />

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

// Shows a new seller exactly what is done and what is left, and goes away once everything is.
function SetupChecklist({ verified, hasDrop }: { verified: boolean; hasDrop: boolean }) {
  const steps = [
    { label: 'Signed in with PayPal', done: true },
    { label: 'Shop opened', done: true },
    { label: 'PayPal account verified', done: verified },
    { label: 'First drop created', done: hasDrop },
  ]
  if (steps.every((x) => x.done)) return null
  return (
    <section className="checklist card" aria-label="Getting started">
      <ol>
        {steps.map((step) => (
          <li key={step.label} className={step.done ? 'check-done' : 'check-todo'}>
            <span className="check-mark" aria-hidden="true">
              {step.done ? '✓' : ''}
            </span>
            <span>{step.label}</span>
            <span className="visually-hidden">{step.done ? ' (done)' : ' (to do)'}</span>
          </li>
        ))}
      </ol>
      {!verified && (
        <p className="note">
          <strong>Your PayPal account isn’t verified yet.</strong> To protect buyers, only verified sellers can open drops. Verify your account with PayPal, then
          sign out and sign back in.
        </p>
      )}
    </section>
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
