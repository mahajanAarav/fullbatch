import { useCallback, useState, type FormEvent } from 'react'
import { cancelDrop, createSeller, sellerDrops, type Drop } from '../api'
import { AssistantDock } from '../components/AssistantDock'
import { Chat } from '../components/Chat'
import { CreateDropDialog } from '../components/CreateDropDialog'
import { DropCard } from '../components/DropCard'
import { Modal } from '../components/Modal'
import { Toast } from '../components/Toast'
import { money } from '../format'
import { usePolling } from '../hooks'
import { forgetSeller, getSeller, saveSeller, sellerSessionId, type SellerIdentity } from '../identity'

export default function Sell() {
  const [seller, setSeller] = useState<SellerIdentity | null>(getSeller)

  if (!seller) {
    return (
      <Onboarding
        onDone={(s) => {
          saveSeller(s)
          setSeller(s)
        }}
      />
    )
  }
  return (
    <Workspace
      seller={seller}
      onSwitch={() => {
        forgetSeller()
        setSeller(null)
      }}
    />
  )
}

function Onboarding({ onDone }: { onDone: (s: SellerIdentity) => void }) {
  const [name, setName] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  async function submit(e: FormEvent) {
    e.preventDefault()
    if (!name.trim() || busy) return
    setBusy(true)
    setError(null)
    try {
      onDone(await createSeller(name.trim()))
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Could not create your shop.')
      setBusy(false)
    }
  }

  return (
    <main className="narrow">
      <form className="card form form-card" onSubmit={submit}>
        <h1>Name your shop</h1>
        <p className="muted">This is how you’ll appear to buyers. You can run as many drops as you like.</p>
        <label>
          Shop name
          <input value={name} onChange={(e) => setName(e.target.value)} placeholder="Maple Street Bakery" maxLength={120} autoFocus />
        </label>
        {error && <p className="error">{error}</p>}
        <button className="button" disabled={!name.trim() || busy}>
          {busy ? 'Creating…' : 'Start selling'}
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

function Workspace({ seller, onSwitch }: { seller: SellerIdentity; onSwitch: () => void }) {
  const { data: drops, error, reload } = usePolling(() => sellerDrops(seller.id))
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
            <h1>{seller.name}</h1>
            <button className="link-button small" onClick={onSwitch}>
              Not you? Switch shop
            </button>
          </div>
          <button className="button" onClick={() => setCreating(true)}>
            + New drop
          </button>
        </div>

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
            <button className="button" onClick={() => setCreating(true)}>
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
          key={seller.id}
          role="seller"
          sellerId={seller.id}
          sessionId={sellerSessionId(seller.id)}
          intro="I can create drops, check progress, or cancel one. You can also use the New drop button."
          suggestions={['How are my drops doing?', 'Help me set up a new drop']}
          onReply={reload}
        />
      </AssistantDock>

      <CreateDropDialog open={creating} sellerId={seller.id} onClose={() => setCreating(false)} onCreated={() => { reload(); setToast('Drop created. It’s open for orders.') }} />

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
