// Everything the frontend knows about the backend lives in this file.

export interface DropSummary {
  id: number
  seller_id: number
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

export const createSeller = (name: string) => post<{ id: number; name: string }>('/sellers', { name })
export const listOpenDrops = () => request<{ drops: Drop[] }>('/drops').then((r) => r.drops)
export const sellerDrops = (sellerId: number) =>
  request<{ drops: Drop[] }>(`/sellers/${sellerId}/drops`).then((r) => r.drops)
export const buyerOrders = (sessionId: string) =>
  request<{ orders: Order[] }>(`/buyers/${encodeURIComponent(sessionId)}/orders`).then((r) => r.orders)
export const getOrder = (id: number) => request<Order>(`/orders/${id}`)

export const chatHistory = (role: 'seller' | 'buyer', sessionId: string) =>
  request<{ messages: ChatLine[] }>(`/chat/${role}/${encodeURIComponent(sessionId)}/history`).then(
    (r) => r.messages,
  )
export const sendSellerChat = (sellerId: number, sessionId: string, message: string) =>
  post<{ reply: string }>('/chat/seller', { seller_id: sellerId, session_id: sessionId, message })
export const sendBuyerChat = (sessionId: string, message: string) =>
  post<{ reply: string }>('/chat/buyer', { session_id: sessionId, message })

export interface NewDrop {
  seller_id: number
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
export const placeOrder = (
  dropId: number,
  body: { buyer_name: string; buyer_email: string; chat_session_id: string; quantity: number },
) => post<PlacedOrder>(`/drops/${dropId}/orders`, body)
