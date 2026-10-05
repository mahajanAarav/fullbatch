import { useEffect, useRef, useState } from 'react'
import { loadPayPal } from '../paypalSdk'

// PayPal's own Pay button, rendered right inside the dialog. If PayPal's script cannot load, the caller's
// "Continue to PayPal" button (a full-page redirect) is still there, so buyers are never stuck.
export function PayPalButton({
  clientId,
  currency,
  create,
  onApproved,
  onCancelled,
  onError,
  onUnavailable,
}: {
  clientId: string
  currency: string
  create: () => Promise<string> // reserves the units and returns PayPal's order id
  onApproved: (paypalOrderId: string) => Promise<void>
  onCancelled: () => void
  onError: (message: string) => void
  onUnavailable: () => void
}) {
  const host = useRef<HTMLDivElement>(null)
  const [ready, setReady] = useState(false)
  // Always call the latest callbacks, without re-rendering PayPal's buttons every time the form changes.
  const latest = useRef({ create, onApproved, onCancelled, onError, onUnavailable })
  useEffect(() => {
    latest.current = { create, onApproved, onCancelled, onError, onUnavailable }
  })

  useEffect(() => {
    let closed = false
    let instance: { close: () => Promise<void> } | null = null
    loadPayPal(clientId, currency)
      .then((paypal) => {
        if (closed || !host.current) return
        const buttons = paypal.Buttons({
          style: { layout: 'vertical', shape: 'rect', label: 'pay', height: 42 },
          createOrder: () => latest.current.create(),
          onApprove: (data) => latest.current.onApproved(data.orderID),
          onCancel: () => latest.current.onCancelled(),
          onError: (err) => latest.current.onError(err instanceof Error ? err.message : 'PayPal ran into a problem. Please try again.'),
        })
        instance = buttons
        return buttons.render(host.current).then(() => setReady(true))
      })
      .catch(() => latest.current.onUnavailable())
    return () => {
      closed = true
      instance?.close().catch(() => {})
    }
  }, [clientId, currency])

  return (
    <div className="paypal-buttons">
      {!ready && <p className="muted small">Loading PayPal…</p>}
      <div ref={host} />
    </div>
  )
}
