import { useEffect, useRef, useState, type FormEvent } from 'react'
import { sendEmailCode, verifyEmailCode, type CodeSent } from '../api'
import { useAuth } from '../authContext'
import { Modal } from './Modal'

// Confirms the person controls their email with a one-time 6-digit code. Emails PayPal already confirmed
// never reach this screen.
export function VerifyEmailDialog({ open, onClose, onVerified }: { open: boolean; onClose: () => void; onVerified?: () => void }) {
  return (
    <Modal open={open} onClose={onClose} title="Verify your email">
      {open && <Body onClose={onClose} onVerified={onVerified} />}
    </Modal>
  )
}

const RESEND_SECONDS = 60

function Body({ onClose, onVerified }: { onClose: () => void; onVerified?: () => void }) {
  const { me, refresh } = useAuth()
  const [sent, setSent] = useState<CodeSent | null>(null)
  const [code, setCode] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [wait, setWait] = useState(0) // seconds until another code may be requested
  const started = useRef(false)

  async function send() {
    setError(null)
    setBusy(true)
    try {
      setSent(await sendEmailCode())
      setWait(RESEND_SECONDS)
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Could not send the code.')
    } finally {
      setBusy(false)
    }
  }

  // Send the first code as soon as the dialog opens (once, even in development's double render).
  useEffect(() => {
    if (started.current) return
    started.current = true
    send()
  }, [])

  useEffect(() => {
    if (wait <= 0) return
    const id = setTimeout(() => setWait((w) => w - 1), 1000)
    return () => clearTimeout(id)
  }, [wait])

  async function submit(e: FormEvent) {
    e.preventDefault()
    if (code.length !== 6 || busy) return
    setBusy(true)
    setError(null)
    try {
      await verifyEmailCode(code)
      await refresh()
      onVerified?.()
      onClose()
    } catch (err) {
      setError(err instanceof Error ? err.message : 'That didn’t work. Please try again.')
      setBusy(false)
    }
  }

  return (
    <form className="form" onSubmit={submit}>
      <p className="muted">
        {sent?.email ? (
          <>
            We sent a 6-digit code to <strong>{sent.email}</strong>. It works for 10 minutes.
          </>
        ) : (
          <>
            We’ll email a 6-digit code to <strong>{me?.user?.email}</strong>.
          </>
        )}
      </p>

      {sent?.dev_code && (
        <p className="note">
          <strong>Local development:</strong> no email is set up, so here’s your code: <code className="dev-code">{sent.dev_code}</code>
        </p>
      )}

      <label>
        Verification code
        <input
          className="code-input"
          value={code}
          onChange={(e) => setCode(e.target.value.replace(/\D/g, '').slice(0, 6))}
          inputMode="numeric"
          autoComplete="one-time-code"
          placeholder="123456"
          maxLength={6}
          autoFocus
        />
      </label>
      {error && (
        <p className="error" role="alert">
          {error}
        </p>
      )}

      <div className="form-actions between">
        <button type="button" className="link-button small" onClick={send} disabled={busy || wait > 0}>
          {wait > 0 ? `Send a new code in ${wait}s` : 'Send a new code'}
        </button>
        <span className="form-actions">
          <button type="button" className="button button-ghost" onClick={onClose}>
            Not now
          </button>
          <button className="button" disabled={busy || code.length !== 6}>
            {busy ? 'Checking…' : 'Verify'}
          </button>
        </span>
      </div>
    </form>
  )
}
