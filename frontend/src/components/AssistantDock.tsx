import type { ReactNode } from 'react'
import { useStoredFlag } from '../hooks'

// The chat assistant lives in a side panel you can hide. Everything it does can also be done
// with the buttons and forms on the page.
export function AssistantDock({ children, label = 'Assistant' }: { children: ReactNode; label?: string }) {
  const [open, setOpen] = useStoredFlag('fullbatch.dock', () => window.innerWidth >= 1100)

  return (
    <>
      <aside className={`dock ${open ? 'dock-open' : ''}`} aria-label={label} hidden={!open}>
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
