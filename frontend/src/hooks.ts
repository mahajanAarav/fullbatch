import { useCallback, useEffect, useRef, useState } from 'react'

// The current time, refreshed every so often, so countdowns keep ticking.
export function useNow(intervalMs = 30_000): number {
  const [now, setNow] = useState(() => Date.now())
  useEffect(() => {
    const id = setInterval(() => setNow(Date.now()), intervalMs)
    return () => clearInterval(id)
  }, [intervalMs])
  return now
}

// Load data now, then keep it fresh. `reload()` forces an immediate refresh
// (used right after the chat agent does something).
export function usePolling<T>(fetcher: () => Promise<T>, interval: number | ((latest: T | null) => number) = 8_000, enabled = true) {
  const [data, setData] = useState<T | null>(null)
  const [error, setError] = useState<string | null>(null)
  const fetcherRef = useRef(fetcher)
  // Keep the latest fetcher without making `reload` change identity every render.
  useEffect(() => {
    fetcherRef.current = fetcher
  })

  const reload = useCallback(async () => {
    try {
      setData(await fetcherRef.current())
      setError(null)
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Could not load.')
    }
  }, [])

  // The interval may depend on the data itself (e.g. poll faster while a payment is pending).
  const intervalMs = typeof interval === 'function' ? interval(data) : interval

  useEffect(() => {
    if (!enabled) return
    reload()
    const id = setInterval(reload, intervalMs)
    return () => clearInterval(id)
  }, [reload, intervalMs, enabled])

  return { data, error, reload }
}

// A boolean remembered in this browser (e.g. whether the assistant panel is open).
export function useStoredFlag(key: string, initial: () => boolean): [boolean, (v: boolean) => void] {
  const [value, setValue] = useState<boolean>(() => {
    try {
      const saved = localStorage.getItem(key)
      if (saved !== null) return saved === '1'
    } catch {
      /* storage blocked */
    }
    return initial()
  })
  const set = useCallback(
    (v: boolean) => {
      setValue(v)
      try {
        localStorage.setItem(key, v ? '1' : '0')
      } catch {
        /* ignore */
      }
    },
    [key],
  )
  return [value, set]
}
