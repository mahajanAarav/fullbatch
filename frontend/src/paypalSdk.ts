// Loads PayPal's JavaScript SDK once. intent=authorize matches how the server creates orders: the buyer
// approves a hold, and we capture it later (or void it) when the drop closes.

interface Actions {
  order?: unknown
}
export interface PayPalButtonsConfig {
  style?: Record<string, string | number | boolean>
  createOrder: () => Promise<string>
  onApprove: (data: { orderID: string }, actions: Actions) => Promise<void>
  onCancel?: () => void
  onError?: (err: unknown) => void
}
interface PayPalNamespace {
  Buttons: (config: PayPalButtonsConfig) => { render: (el: HTMLElement) => Promise<void>; close: () => Promise<void> }
}

declare global {
  interface Window {
    paypal?: PayPalNamespace
  }
}

let loading: Promise<PayPalNamespace> | null = null

export function loadPayPal(clientId: string, currency: string): Promise<PayPalNamespace> {
  if (window.paypal) return Promise.resolve(window.paypal)
  loading ??= new Promise<PayPalNamespace>((resolve, reject) => {
    const script = document.createElement('script')
    script.src = `https://www.paypal.com/sdk/js?client-id=${encodeURIComponent(clientId)}&currency=${currency}&intent=authorize&components=buttons`
    script.async = true
    script.onload = () => (window.paypal ? resolve(window.paypal) : reject(new Error('PayPal did not load.')))
    script.onerror = () => {
      loading = null // allow a retry later
      reject(new Error('Could not load PayPal.'))
    }
    document.head.appendChild(script)
  })
  return loading
}
