// Everything the frontend knows about the backend lives in this file.

export interface DropSummary {
  id: number
  seller_id: number
  shop_name: string
  offers_pickup: boolean
  offers_delivery: boolean
  area: string | null // a public neighborhood label; never the exact address
  lat: number | null // rounded to ~1 km
  lng: number | null
  delivery_radius_km: number | null
  delivery_fee: string
  item_name: string
  unit_price: string
  currency: string
  quantity_total: number
  minimum_units: number
  max_per_buyer: number
  deadline: string
  status: 'open' | 'filled' | 'failed' | 'cancelled'
  units_taken: number
  units_remaining: number
}

// A drop plus how far along it is toward its minimum.
export interface Drop extends DropSummary {
  units_by_order_status: Record<string, number>
  paid_up_units: number
  minimum_met_so_far: boolean
}

export type OrderStatus = 'reserved' | 'authorized' | 'captured' | 'voided' | 'expired' | 'failed'

export interface Order {
  id: number
  can_pay: boolean // reserved and still within its time to pay
  status: OrderStatus
  quantity: number
  amount: string
  currency: string
  reserved_until: string
  drop_id: number
  item_name: string
  drop_status: DropSummary['status']
  deadline: string
  minimum_units: number
  paid_up_units: number
  fulfillment: 'pickup' | 'delivery'
  delivery_fee: string
  area: string | null
  pickup_address: string | null // only once the hold is approved
  pickup_notes: string | null // only once the hold is approved
  delivery_address: string | null
  paypal: { order_id: string | null; authorization_id: string | null; hold_expires: string | null; capture_id: string | null }
  timeline: { kind: string; label: string; via: string | null; at: string }[]
}

export interface ChatLine {
  role: 'user' | 'assistant'
  text: string
}

export class ApiError extends Error {
  status: number
  constructor(status: number, message: string) {
    super(message)
    this.status = status
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  let res: Response
  try {
    res = await fetch(`/api${path}`, init)
  } catch {
    throw new ApiError(0, "Can't reach the server. Is it running?")
  }
  if (!res.ok) {
    let message = `Something went wrong (${res.status}).`
    try {
      const body = await res.json()
      if (typeof body.error === 'string') message = body.error
      else if (typeof body.detail === 'string') message = body.detail
    } catch {
      /* keep the generic message */
    }
    throw new ApiError(res.status, message)
  }
  return res.json() as Promise<T>
}

const post = <T>(path: string, body: unknown) =>
  request<T>(path, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  })

export interface Me {
  user: { id: number; name: string; email: string; paypal_verified: boolean; email_verified: boolean; is_dev: boolean } | null
  shop: { id: number; name: string; verified: boolean } | null
  dev_login: boolean // the server allows the local-only dev sign-in
}

export const getMe = () => request<Me>('/auth/me')
export const devLogin = (body: { name: string; email: string; verified: boolean; email_verified: boolean }) => post<Me>('/auth/dev-login', body)
export interface CodeSent {
  sent?: boolean
  already_verified?: boolean
  email?: string // the address, partly hidden
  dev_code?: string // local development only, when no email is set up
}
export const sendEmailCode = () => post<CodeSent>('/auth/email/send', {})
export const verifyEmailCode = (code: string) => post<Me>('/auth/email/verify', { code })
export const logout = () => post<{ ok: boolean }>('/auth/logout', {})
export const createShop = (name: string) => post<{ id: number; name: string; verified: boolean }>('/shop', { name })

export const listOpenDrops = () => request<{ drops: Drop[] }>('/drops').then((r) => r.drops)
export const myDrops = () => request<{ drops: Drop[] }>('/me/drops').then((r) => r.drops)
export const myOrders = () => request<{ orders: Order[] }>('/me/orders').then((r) => r.orders)
export const getOrder = (id: number) => request<Order>(`/orders/${id}`)

export const chatHistory = (role: 'seller' | 'buyer') =>
  request<{ messages: ChatLine[] }>(`/chat/${role}/history`).then((r) => r.messages)
export const sendChat = (role: 'seller' | 'buyer', message: string) =>
  post<{ reply: string }>(`/chat/${role}`, { message })

export interface NewDrop {
  item_name: string
  unit_price: string
  quantity_total: number
  minimum_units: number
  max_per_buyer: number
  deadline: string // ISO 8601 with a timezone
  pickup_address: string
  pickup_notes: string | null
  offers_pickup: boolean
  offers_delivery: boolean
  delivery_radius_miles: number | null
  delivery_fee: string
}
export const createDrop = (drop: NewDrop) => post<Drop>('/drops', drop)
export const cancelDrop = (id: number) => post<{ id: number; status: string }>(`/drops/${id}/cancel`, {})

export interface PlacedOrder {
  order_id: number
  paypal_order_id: string
  approval_url: string
  amount: string
  reserved_until: string
}
export const placeOrder = (
  dropId: number,
  quantity: number,
  fulfillment: 'pickup' | 'delivery' = 'pickup',
  deliveryAddress?: string,
) => post<PlacedOrder>(`/drops/${dropId}/orders`, { quantity, fulfillment, delivery_address: deliveryAddress })

// Lets a seller confirm an address was understood before they post a drop with it.
export const checkAddress = (query: string) => post<{ area: string; label: string }>('/geo/check', { query })

export interface FulfillmentRow {
  order_id: number
  buyer: string // first name and last initial only
  quantity: number
  status: OrderStatus
  fulfillment: 'pickup' | 'delivery'
  delivery_address: string | null
  amount: string
}
export const dropOrders = (dropId: number) => request<{ orders: FulfillmentRow[] }>(`/me/drops/${dropId}/orders`).then((r) => r.orders)

// Flat rows for the AG Studio dashboard. Buyers' identities are never included.
export interface AnalyticsDrop {
  drop_id: number
  item_name: string
  status: Drop['status']
  is_open: number
  unit_price: number
  quantity_total: number
  minimum_units: number
  units_approved: number
  units_reserved: number
  fill_percent: number
  deadline: string
  created_at: string
}
export interface AnalyticsOrder {
  order_id: number
  drop_id: number
  item_name: string
  status: OrderStatus
  quantity: number
  amount: number
  created_at: string
  units_approved: number
  value_approved: number
  amount_on_hold: number
  amount_collected: number
}
export const myAnalytics = () => request<{ drops: AnalyticsDrop[]; orders: AnalyticsOrder[] }>('/me/analytics')

// The drop planner: how finished drops went, and a suggestion for the next one.
export interface PlanReport {
  drop_id: number
  item_name: string
  status: 'filled' | 'failed'
  unit_price: number
  quantity_total: number
  minimum_units: number
  max_per_buyer: number
  committed_units: number
  committed_orders: number
  average_order_size: number
  sell_through: number
  minimum_ratio: number
  window_hours: number
  hours_to_minimum: number | null
  share_in_last_day: number
  revenue_collected: number
  revenue_not_collected: number
  busiest_weekday: string | null
  busiest_hour: number | null
  closed_at: string
}
export interface Recommendation {
  enough_data: boolean
  confidence: 'none' | 'low' | 'medium' | 'high'
  message?: string
  caveat?: string | null
  recommended: {
    item_name: string | null
    unit_price: number | null
    quantity_total: number
    minimum_units: number
    max_per_buyer: number
    duration_days: number
    deadline?: string
  }
  reasons: string[]
  based_on: number[]
}
export const myPlan = () => request<{ recommendation: Recommendation; reports: PlanReport[] }>('/me/plan')

// Pick up an unfinished checkout: the PayPal link for a reservation that is still held.
export const payOrder = (id: number) => post<{ approval_url: string }>(`/orders/${id}/pay`, {})
// Give up a reservation that hasn't been paid for.
export const cancelOrder = (id: number) => post<Order>(`/orders/${id}/cancel`, {})

// A shop's public page: built from real activity, with no addresses, emails or buyer names.
export interface Shop {
  id: number
  name: string
  verified: boolean
  member_since: string
  area: string | null
  drops_filled: number
  drops_total: number
  units_delivered: number
  open_drops: Drop[]
}
export const getShop = (id: number) => request<Shop>(`/shops/${id}`)


export interface PayoutRow {
  drop_id: number
  item_name: string
  gross: string
  fee: string
  net: string
  currency: string
  status: 'pending' | 'success' | 'unclaimed' | 'denied' | 'unavailable' | 'skipped'
  detail: string | null
  batch_id: string | null
  updated_at: string
}
export const myPayouts = () => request<{ fee_percent: number; payouts: PayoutRow[] }>('/me/payouts')


export const getConfig = () => request<{ paypal_client_id: string | null; currency: string }>('/config')
export const confirmOrder = (id: number) => post<Order>(`/orders/${id}/confirm`, {})
