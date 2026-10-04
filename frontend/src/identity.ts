// There are no logins. The browser remembers who you are in localStorage.

const BUYER_KEY = 'fullbatch.buyerSession'
const SELLER_KEY = 'fullbatch.seller'

export interface SellerIdentity {
  id: number
  name: string
}

function read(key: string): string | null {
  try {
    return localStorage.getItem(key)
  } catch {
    return null // storage blocked (private mode): fall back to in-memory identity
  }
}

function write(key: string, value: string) {
  try {
    localStorage.setItem(key, value)
  } catch {
    /* ignore */
  }
}

let memoryBuyerSession: string | null = null

// A random id, kept in this browser. Orders and chat history are tied to it.
export function getBuyerSessionId(): string {
  const existing = read(BUYER_KEY) ?? memoryBuyerSession
  if (existing) return existing
  const fresh = `buyer-${crypto.randomUUID()}`
  memoryBuyerSession = fresh
  write(BUYER_KEY, fresh)
  return fresh
}

export function getSeller(): SellerIdentity | null {
  const raw = read(SELLER_KEY)
  if (!raw) return null
  try {
    const parsed = JSON.parse(raw)
    return typeof parsed.id === 'number' && typeof parsed.name === 'string' ? parsed : null
  } catch {
    return null
  }
}

export function saveSeller(seller: SellerIdentity) {
  write(SELLER_KEY, JSON.stringify(seller))
}

export function forgetSeller() {
  try {
    localStorage.removeItem(SELLER_KEY)
  } catch {
    /* ignore */
  }
}

// One stable chat session per seller, so their conversation survives a reload.
export const sellerSessionId = (sellerId: number) => `seller-${sellerId}`
