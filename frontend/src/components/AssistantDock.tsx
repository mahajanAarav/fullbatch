import type { ReactNode } from 'react'
import { useStoredFlag } from '../hooks'

// The chat assistant lives in a side panel you can hide. Everything it does can also be done
// with the buttons and forms on the page.
// `overlay` floats the panel over the page (used when the page itself needs the full width, like the dashboard).
export function AssistantDock({ children, label = 'Assistant', overlay = false }: { children: ReactNode; label?: string; overlay?: boolean }) {
  const [open, setOpen] = useStoredFlag(overlay ? 'fullbatch.dock.overlay' : 'fullbatch.dock', () => false) // closed until the user opens it; their choice is then remembered

  return (
    <>
      <aside className={`dock ${open ? 'dock-open' : ''} ${overlay ? 'dock-overlay' : ''}`} aria-label={label} hidden={!open}>
        <header className="dock-head">
          <div>
            <strong>{label}</strong>
            <span className="muted small"> · optional</span>
          </div>
          <button className="icon-button" onClick={() => setOpen(false)} aria-label="Hide assistant">
            ×
          </button>
        </header>
        {children}
      </aside>
      {!open && (
        <button className="dock-fab button" onClick={() => setOpen(true)}>
          Ask the assistant
        </button>
      )}
    </>
  )
}
