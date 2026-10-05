// Everything the frontend knows about the backend lives in this file.

export interface DropSummary {
  id: number
  seller_id: number
  shop_name: string
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
}
export const createDrop = (drop: NewDrop) => post<Drop>('/drops', drop)
export const cancelDrop = (id: number) => post<{ id: number; status: string }>(`/drops/${id}/cancel`, {})

export interface PlacedOrder {
  order_id: number
  approval_url: string
  amount: string
  reserved_until: string
}
export const placeOrder = (dropId: number, quantity: number) => post<PlacedOrder>(`/drops/${dropId}/orders`, { quantity })

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
