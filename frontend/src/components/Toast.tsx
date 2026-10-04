import { useEffect } from 'react'

// A short notice that fades away by itself.
export function Toast({ message, onDone }: { message: string | null; onDone: () => void }) {
  useEffect(() => {
    if (!message) return
    const id = setTimeout(onDone, 8000)
    return () => clearTimeout(id)
  }, [message, onDone])

  if (!message) return null
  return (
    <div className="toast" role="status">
      <span>{message}</span>
      <button className="icon-button" onClick={onDone} aria-label="Dismiss">
        ×
      </button>
    </div>
  )
}
