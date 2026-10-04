import { useState, type FormEvent } from 'react'
import { createSeller, sellerDrops } from '../api'
import { Chat } from '../components/Chat'
import { DropCard } from '../components/DropCard'
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
      <form className="card form" onSubmit={submit}>
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

function Workspace({ seller, onSwitch }: { seller: SellerIdentity; onSwitch: () => void }) {
  const { data: drops, error, reload } = usePolling(() => sellerDrops(seller.id))

  return (
    <main className="workspace">
      <div className="workspace-head">
        <div>
          <p className="eyebrow">Seller</p>
          <h1>{seller.name}</h1>
        </div>
        <button className="link-button" onClick={onSwitch}>
          Not you? Switch shop
        </button>
      </div>

      <div className="columns">
        <Chat
          key={seller.id}
          role="seller"
          sellerId={seller.id}
          sessionId={sellerSessionId(seller.id)}
          intro="Tell me what you’d like to sell and I’ll set up a drop. I’ll ask for anything I’m missing."
          suggestions={['I want to open a new drop', 'How are my drops doing?']}
          onReply={reload}
        />

        <aside className="panel">
          <h2>Your drops</h2>
          {error && <p className="error">{error}</p>}
          {drops && drops.length === 0 && <p className="muted empty">No drops yet. Start one in the chat.</p>}
          {drops?.map((d) => <DropCard key={d.id} drop={d} />)}
        </aside>
      </div>
    </main>
  )
}
