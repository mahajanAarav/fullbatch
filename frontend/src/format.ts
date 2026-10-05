export function money(amount: string | number, currency = 'USD'): string {
  return new Intl.NumberFormat(undefined, { style: 'currency', currency }).format(Number(amount)) // the visitor's own number format
}

export function whenText(iso: string): string {
  return new Date(iso).toLocaleString([], {
    weekday: 'short',
    month: 'short',
    day: 'numeric',
    hour: 'numeric',
    minute: '2-digit',
  })
}

// "2d 4h left", "35m left", "Closed"
export function timeLeft(iso: string, now: number): string {
  const ms = new Date(iso).getTime() - now
  if (ms <= 0) return 'Closed'
  const minutes = Math.floor(ms / 60_000)
  const days = Math.floor(minutes / 1440)
  const hours = Math.floor((minutes % 1440) / 60)
  if (days > 0) return `${days}d ${hours}h left`
  if (hours > 0) return `${hours}h ${minutes % 60}m left`
  return `${Math.max(minutes, 1)}m left`
}
