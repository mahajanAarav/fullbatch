import { useState, type FormEvent } from 'react'
import { useNavigate, useSearchParams } from 'react-router-dom'
import { devLogin } from '../api'
import { signInUrl, useAuth } from '../authContext'

const ERRORS: Record<string, string> = {
  cancelled: 'Sign-in was cancelled. You can try again whenever you’re ready.',
  state: 'That sign-in link expired or didn’t match. Please start again.',
  paypal: 'PayPal couldn’t confirm your identity right now. Please try again.',
}

// Only ever go back to a path on this site.
const safeNext = (value: string | null) => (value && value.startsWith('/') && !value.startsWith('//') ? value : '/')

export default function SignIn() {
  const [params] = useSearchParams()
  const next = safeNext(params.get('next'))
  const error = params.get('error')
  const { me, refresh } = useAuth()
  const navigate = useNavigate()

  return (
    <main className="narrow">
      <div className="card form-card signin">
        <h1>Sign in to fullbatch</h1>
        <p className="muted">
          We use PayPal to confirm who you are, so buyers and sellers can trust each other. Sellers need a verified PayPal account to open drops.
        </p>
        {error && (
          <p className="error" role="alert">
            {ERRORS[error] ?? 'Something went wrong signing in.'}
          </p>
        )}
        <a className="button button-paypal button-block" href={signInUrl(next)}>
          Continue with PayPal
        </a>
        <p className="muted small">We only receive your name, email and whether PayPal has verified your account. We never see your password.</p>

        {me?.dev_login && (
          <DevSignIn
            onDone={async () => {
              await refresh()
              navigate(next, { replace: true })
            }}
          />
        )}
      </div>
    </main>
  )
}

// Local testing only: the server answers 404 to this unless DEV_LOGIN=1, and the deployed app never sets it.
function DevSignIn({ onDone }: { onDone: () => Promise<void> }) {
  const [name, setName] = useState('Dev Tester')
  const [email, setEmail] = useState('dev@example.com')
  const [verified, setVerified] = useState(true)
  const [emailVerified, setEmailVerified] = useState(true)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  async function submit(e: FormEvent) {
    e.preventDefault()
    setBusy(true)
    setError(null)
    try {
      await devLogin({ name: name.trim(), email: email.trim(), verified, email_verified: emailVerified })
      await onDone()
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Could not sign in.')
      setBusy(false)
    }
  }

  return (
    <form className="dev-box form" onSubmit={submit}>
      <div className="dev-head">
        <strong>Developer sign-in</strong>
        <span className="pill">Local only</span>
      </div>
      <label>
        Name
        <input value={name} onChange={(e) => setName(e.target.value)} required maxLength={120} />
      </label>
      <label>
        Email
        <input type="email" value={email} onChange={(e) => setEmail(e.target.value)} required />
      </label>
      <label className="check">
        <input type="checkbox" checked={verified} onChange={(e) => setVerified(e.target.checked)} />
        Pretend this is a PayPal-verified account
      </label>
      <label className="check">
        <input type="checkbox" checked={emailVerified} onChange={(e) => setEmailVerified(e.target.checked)} />
        Email already confirmed (untick to try the email code)
      </label>
      {error && <p className="error" role="alert">{error}</p>}
      <button className="button button-ghost" disabled={busy}>
        {busy ? 'Signing in…' : 'Sign in (dev)'}
      </button>
    </form>
  )
}
