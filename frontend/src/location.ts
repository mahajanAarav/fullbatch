import { useCallback, useEffect, useState } from 'react'

// The buyer's location (a ZIP code or "use my location") stays in THIS browser. It is only used to
// sort and filter drops by distance and is never sent to our server.

export interface BuyerLocation {
  lat: number
  lng: number
  label: string
}

const KEY = 'fullbatch.location'
export const KM_PER_MILE = 1.609344

function read(): BuyerLocation | null {
  try {
    const parsed = JSON.parse(localStorage.getItem(KEY) ?? 'null')
    return parsed && typeof parsed.lat === 'number' && typeof parsed.lng === 'number' ? parsed : null
  } catch {
    return null
  }
}

export function useBuyerLocation(): [BuyerLocation | null, (loc: BuyerLocation | null) => void] {
  const [location, setLocation] = useState<BuyerLocation | null>(read)
  const set = useCallback((loc: BuyerLocation | null) => {
    setLocation(loc)
    try {
      if (loc) localStorage.setItem(KEY, JSON.stringify(loc))
      else localStorage.removeItem(KEY)
    } catch {
      /* storage blocked: it just won't be remembered */
    }
  }, [])
  useEffect(() => {
    const onStorage = () => setLocation(read())
    window.addEventListener('storage', onStorage)
    return () => window.removeEventListener('storage', onStorage)
  }, [])
  return [location, set]
}

// Great-circle distance in miles.
export function distanceMiles(a: { lat: number; lng: number }, b: { lat: number; lng: number }): number {
  const rad = (d: number) => (d * Math.PI) / 180
  const dLat = rad(b.lat - a.lat)
  const dLng = rad(b.lng - a.lng)
  const h = Math.sin(dLat / 2) ** 2 + Math.cos(rad(a.lat)) * Math.cos(rad(b.lat)) * Math.sin(dLng / 2) ** 2
  return (2 * 6371.0088 * Math.asin(Math.sqrt(h))) / KM_PER_MILE
}

// A US ZIP code to a place, using the free zippopotam.us service straight from the browser.
export async function lookupZip(zip: string): Promise<BuyerLocation> {
  const clean = zip.trim()
  if (!/^\d{5}$/.test(clean)) throw new Error('Enter a 5-digit US ZIP code.')
  let res: Response
  try {
    res = await fetch(`https://api.zippopotam.us/us/${clean}`)
  } catch {
    throw new Error('Couldn’t look that up right now. Check your connection and try again.')
  }
  if (res.status === 404) throw new Error('We couldn’t find that ZIP code.')
  if (!res.ok) throw new Error('The ZIP lookup is unavailable right now.')
  const body = await res.json()
  const place = body.places?.[0]
  if (!place) throw new Error('We couldn’t find that ZIP code.')
  return { lat: Number(place.latitude), lng: Number(place.longitude), label: `${place['place name']}, ${place['state abbreviation']} ${clean}` }
}

// The browser's own position, with the browser asking the person for permission.
export function currentPosition(): Promise<BuyerLocation> {
  return new Promise((resolve, reject) => {
    if (!navigator.geolocation) return reject(new Error('Your browser can’t share its location.'))
    navigator.geolocation.getCurrentPosition(
      (p) => resolve({ lat: p.coords.latitude, lng: p.coords.longitude, label: 'your location' }),
      (e) => reject(new Error(e.code === e.PERMISSION_DENIED ? 'Location access was declined. You can enter a ZIP code instead.' : 'Couldn’t get your location.')),
      { timeout: 10_000, maximumAge: 600_000 },
    )
  })
}
