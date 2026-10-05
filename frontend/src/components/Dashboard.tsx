import { useMemo, useState } from 'react'
import { AgStudio } from 'ag-studio-react'
import { studioTheme } from 'ag-studio'
import type { AgDataSourcesDefinition, AgReportState, AgStudioMode } from 'ag-studio'
import { myAnalytics } from '../api'
import { usePolling } from '../hooks'

// AG Studio computes everything in the browser from plain arrays. We give it two tables
// (drops, orders) and describe a finished dashboard as data, so the seller sees it immediately.

const usd = { format: new Intl.NumberFormat('en-US', { style: 'currency', currency: 'USD' }) }

const prefersDark = () => window.matchMedia('(prefers-color-scheme: dark)').matches

function buildTheme(dark: boolean) {
  // The same warm palette as the rest of the app.
  const p = dark
    ? { bg: '#26201a', canvas: '#1c1712', ink: '#f4ece1', muted: '#b3a595', line: '#3a3228' }
    : { bg: '#ffffff', canvas: '#fbf6ee', ink: '#2a2118', muted: '#6f6256', line: '#e9dfd0' }
  // Chart series colors: terracotta, green, amber, brown, sage, rose. Same family as the app.
  const series = dark
    ? ['#e0714f', '#5bc08f', '#e5b04e', '#c08a6b', '#8fbf9f', '#ee7c74']
    : ['#c4512f', '#2f7d5b', '#d89a2b', '#8a5a44', '#7fa88f', '#b3372f']
  return studioTheme.withParams({
    chartPaletteFills1Color: series[0],
    chartPaletteFills2Color: series[1],
    chartPaletteFills3Color: series[2],
    chartPaletteFills4Color: series[3],
    chartPaletteFills5Color: series[4],
    chartPaletteFills6Color: series[5],
    chartTextColor: p.ink,
    chartSubtleTextColor: p.muted,
    chartGridLineColor: p.line,
    chartAxisLineColor: p.line,
    accentColor: dark ? '#e0714f' : '#c4512f',
    backgroundColor: p.bg,
    borderColor: p.line,
    textColor: p.ink,
    fontFamily: 'system-ui, -apple-system, "Segoe UI", Roboto, sans-serif',
    browserColorScheme: dark ? 'dark' : 'light',
    studioCanvasBackgroundColor: p.canvas,
  })
}

const INITIAL_STATE: AgReportState = {
  selectedPageId: 'overview',
  pages: [
    {
      id: 'overview',
      widgets: {
        'kpi-collected': {
          type: 'value',
          dataMapping: { value: [{ id: 'orders.amount_collected', aggregation: 'sum' }] },
          format: { caption: { enabled: true, text: 'Collected' } },
        },
        'kpi-on-hold': {
          type: 'value',
          dataMapping: { value: [{ id: 'orders.amount_on_hold', aggregation: 'sum' }] },
          format: { caption: { enabled: true, text: 'On hold (not charged yet)' } },
        },
        'kpi-units': {
          type: 'value',
          dataMapping: { value: [{ id: 'orders.units_approved', aggregation: 'sum' }] },
          format: { caption: { enabled: true, text: 'Units approved' } },
        },
        'kpi-open': {
          type: 'value',
          dataMapping: { value: [{ id: 'drops.is_open', aggregation: 'sum' }] },
          format: { caption: { enabled: true, text: 'Open drops' } },
        },
        'order-value-over-time': {
          type: 'line-chart',
          dataMapping: {
            categoryKey: [{ id: 'orders.created_at::day' }],
            valueKey: [{ id: 'orders.value_approved', aggregation: 'sum' }],
          },
          format: { caption: { enabled: true, text: 'Approved order value per day' } },
        },
        'orders-by-status': {
          type: 'donut-chart',
          dataMapping: {
            categoryKey: [{ id: 'orders.status' }],
            valueKey: [{ id: 'orders.order_id', aggregation: 'count' }],
          },
          format: { caption: { enabled: true, text: 'Orders by status' } },
        },
        'units-by-drop': {
          type: 'column-chart-grouped',
          dataMapping: {
            categoryKey: [{ id: 'drops.item_name' }],
            valueKey: [
              { id: 'drops.units_approved', aggregation: 'sum' },
              { id: 'drops.minimum_units', aggregation: 'sum' },
              { id: 'drops.quantity_total', aggregation: 'sum' },
            ],
          },
          format: { caption: { enabled: true, text: 'Approved units vs. minimum and total, by drop' } },
        },
        'drops-grid': {
          type: 'grid',
          dataMapping: {
            cols: [
              { id: 'drops.item_name' },
              { id: 'drops.status' },
              { id: 'drops.unit_price' },
              { id: 'drops.units_approved' },
              { id: 'drops.minimum_units' },
              { id: 'drops.quantity_total' },
              { id: 'drops.fill_percent' },
              { id: 'drops.deadline' },
            ],
          },
          format: { caption: { enabled: true, text: 'Your drops' } },
        },
      },
      // A 24-column canvas. Spans are in columns (x) and rows (y).
      widgetLayout: {
        'kpi-collected': { xTrack: 0, yTrack: 0, xSpan: 6, ySpan: 6 },
        'kpi-on-hold': { xTrack: 6, yTrack: 0, xSpan: 6, ySpan: 6 },
        'kpi-units': { xTrack: 12, yTrack: 0, xSpan: 6, ySpan: 6 },
        'kpi-open': { xTrack: 18, yTrack: 0, xSpan: 6, ySpan: 6 },
        'order-value-over-time': { xTrack: 0, yTrack: 6, xSpan: 15, ySpan: 16 },
        'orders-by-status': { xTrack: 15, yTrack: 6, xSpan: 9, ySpan: 16 },
        'units-by-drop': { xTrack: 0, yTrack: 22, xSpan: 12, ySpan: 18 },
        'drops-grid': { xTrack: 12, yTrack: 22, xSpan: 12, ySpan: 18 },
      },
    },
  ],
  panels: { filters: { collapsed: true } },
}

export default function Dashboard() {
  const { data, error } = usePolling(myAnalytics, 15_000)
  const [mode, setMode] = useState<AgStudioMode>('view')
  const theme = useMemo(() => buildTheme(prefersDark()), [])

  const sources = useMemo<AgDataSourcesDefinition | null>(() => {
    if (!data) return null
    const asDate = <T extends Record<string, unknown>>(rows: T[], keys: string[]) =>
      rows.map((r) => ({ ...r, ...Object.fromEntries(keys.map((k) => [k, new Date(r[k] as string)])) }))
    return {
      sources: [
        {
          id: 'drops',
          name: 'Drops',
          data: asDate(data.drops as unknown as Record<string, unknown>[], ['deadline', 'created_at']),
          fields: [
            { id: 'drop_id', name: 'Drop', format: 'integerFormat' },
            { id: 'item_name', name: 'Item', format: 'textFormat' },
            { id: 'status', name: 'Status', format: 'textFormat' },
            { id: 'is_open', name: 'Open', format: 'integerFormat' },
            { id: 'unit_price', name: 'Price', format: 'currencyFormat', formatOptions: usd },
            { id: 'quantity_total', name: 'Total units', format: 'integerFormat' },
            { id: 'minimum_units', name: 'Minimum units', format: 'integerFormat' },
            { id: 'units_approved', name: 'Approved units', format: 'integerFormat' },
            { id: 'units_reserved', name: 'Reserved units', format: 'integerFormat' },
            { id: 'fill_percent', name: 'Progress to minimum (%)', format: 'integerFormat' },
            { id: 'deadline', name: 'Closes', format: 'dateTimeFormat' },
            { id: 'created_at', name: 'Created', format: 'dateTimeFormat' },
          ],
        },
        {
          id: 'orders',
          name: 'Orders',
          data: asDate(data.orders as unknown as Record<string, unknown>[], ['created_at']),
          fields: [
            { id: 'order_id', name: 'Order', format: 'integerFormat' },
            { id: 'drop_id', name: 'Drop', format: 'integerFormat' },
            { id: 'item_name', name: 'Item', format: 'textFormat' },
            { id: 'status', name: 'Status', format: 'textFormat' },
            { id: 'quantity', name: 'Quantity', format: 'integerFormat' },
            { id: 'amount', name: 'Order total', format: 'currencyFormat', formatOptions: usd },
            { id: 'created_at', name: 'Ordered', format: 'dateTimeFormat' },
            { id: 'units_approved', name: 'Approved units', format: 'integerFormat' },
            { id: 'value_approved', name: 'Approved value', format: 'currencyFormat', formatOptions: usd },
            { id: 'amount_on_hold', name: 'On hold', format: 'currencyFormat', formatOptions: usd },
            { id: 'amount_collected', name: 'Collected', format: 'currencyFormat', formatOptions: usd },
          ],
        },
      ],
      relationships: [
        {
          id: 'order-drop',
          source: { tableId: 'orders', fieldId: 'drop_id' },
          target: { tableId: 'drops', fieldId: 'drop_id' },
          type: 'many-to-one',
          acceptFanout: true,
        },
      ],
    }
  }, [data])

  if (error && !data) return <p className="error">{error}</p>
  if (!sources) return <p className="muted">Loading your dashboard…</p>

  return (
    <section className="dashboard card" aria-label="Seller dashboard">
      <div className="dashboard-bar">
        <span className="muted small">
          {mode === 'view' ? 'Click a chart to filter the rest of the page.' : 'Drag, resize and add widgets.'}
        </span>
        <button className="button button-ghost button-small" onClick={() => setMode(mode === 'view' ? 'edit' : 'view')}>
          {mode === 'view' ? 'Customize' : 'Done'}
        </button>
      </div>
      <div className="dashboard-canvas">
        <AgStudio data={sources} initialState={INITIAL_STATE} mode={mode} theme={theme} />
      </div>
    </section>
  )
}
