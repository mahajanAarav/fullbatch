import { useEffect, useRef } from 'react'
import L from 'leaflet'
import 'leaflet/dist/leaflet.css'
import type { Drop } from '../api'
import { money } from '../format'
import type { BuyerLocation } from '../location'

// Open drops on an OpenStreetMap map. Pins sit at the PUBLIC, rounded location (about a kilometre), never the
// exact address. Popups are built from text nodes, so an item name can never inject markup.

interface Props {
  drops: Drop[]
  location: BuyerLocation | null
  onReserve: (drop: Drop) => void
  onOpenShop: (shopId: number) => void
}

function el<K extends keyof HTMLElementTagNameMap>(tag: K, className?: string, text?: string): HTMLElementTagNameMap[K] {
  const node = document.createElement(tag)
  if (className) node.className = className
  if (text !== undefined) node.textContent = text
  return node
}

export function DropsMap({ drops, location, onReserve, onOpenShop }: Props) {
  const host = useRef<HTMLDivElement>(null)
  const map = useRef<L.Map | null>(null)
  const layer = useRef<L.LayerGroup | null>(null)
  const handlers = useRef({ onReserve, onOpenShop })
  useEffect(() => {
    handlers.current = { onReserve, onOpenShop }
  })

  // Create the map once.
  useEffect(() => {
    if (!host.current) return
    const m = L.map(host.current, { scrollWheelZoom: false }).setView([39.8, -98.6], 4)
    L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png', {
      maxZoom: 18,
      attribution: '© <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors',
    }).addTo(m)
    layer.current = L.layerGroup().addTo(m)
    map.current = m
    return () => {
      m.remove()
      map.current = null
    }
  }, [])

  // Redraw the pins whenever the drops or the viewer's location change.
  useEffect(() => {
    const m = map.current
    const group = layer.current
    if (!m || !group) return
    group.clearLayers()

    const spots = new Map<string, Drop[]>()
    for (const d of drops) {
      if (d.lat === null || d.lng === null) continue
      const key = `${d.lat},${d.lng}`
      spots.set(key, [...(spots.get(key) ?? []), d])
    }

    const points: L.LatLngExpression[] = []
    for (const list of spots.values()) {
      const { lat, lng } = list[0]
      const at: L.LatLngExpression = [lat as number, lng as number]
      points.push(at)

      const label = list.length === 1 ? money(list[0].unit_price, list[0].currency) : `${list.length} drops`
      const pin = L.marker(at, { icon: L.divIcon({ className: 'map-pin-wrap', html: `<span class="map-pin">${label}</span>`, iconSize: [0, 0] }) })

      const body = el('div', 'map-popup')
      for (const d of list) {
        const row = el('div', 'map-popup-row')
        row.append(el('strong', undefined, d.item_name))
        const shop = el('button', 'map-link', `by ${d.shop_name}`)
        shop.type = 'button'
        shop.onclick = () => handlers.current.onOpenShop(d.seller_id)
        row.append(shop)
        row.append(el('span', 'map-meta', `${money(d.unit_price, d.currency)} · ${d.units_remaining} left${d.area ? ` · ${d.area}` : ''}`))
        const reserve = el('button', 'map-reserve', d.units_remaining === 0 ? 'Sold out' : 'Reserve')
        reserve.type = 'button'
        reserve.disabled = d.units_remaining === 0
        reserve.onclick = () => handlers.current.onReserve(d)
        row.append(reserve)
        body.append(row)
      }
      pin.bindPopup(body, { minWidth: 200 }).addTo(group)
    }

    if (location) {
      const here: L.LatLngExpression = [location.lat, location.lng]
      L.circleMarker(here, { radius: 8, color: '#1a73e8', fillColor: '#4285f4', fillOpacity: 0.9, weight: 3 }).bindTooltip('You are here').addTo(group)
      points.push(here)
    }

    if (points.length > 1) m.fitBounds(L.latLngBounds(points), { padding: [40, 40], maxZoom: 14 })
    else if (points.length === 1) m.setView(points[0], 13)
  }, [drops, location])

  return <div ref={host} className="map" role="region" aria-label="Map of open drops" />
}
