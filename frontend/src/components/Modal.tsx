import { useEffect, useRef, type ReactNode } from 'react'

// A dialog built on the browser's native <dialog>: it traps focus, closes on Escape,
// and dims the page, with no extra library.
export function Modal({ open, onClose, title, children }: { open: boolean; onClose: () => void; title: string; children: ReactNode }) {
  const ref = useRef<HTMLDialogElement>(null)

  useEffect(() => {
    const dialog = ref.current
    if (!dialog) return
    if (open && !dialog.open) dialog.showModal()
    if (!open && dialog.open) dialog.close()
  }, [open])

  return (
    <dialog
      ref={ref}
      className="modal"
      onClose={onClose}
      onClick={(e) => {
        if (e.target === ref.current) onClose() // a click on the dimmed backdrop
      }}
    >
      {open && (
        <div className="modal-body">
          <header className="modal-head">
            <h2>{title}</h2>
            <button className="icon-button" onClick={onClose} aria-label="Close">
              ×
            </button>
          </header>
          {children}
        </div>
      )}
    </dialog>
  )
}
