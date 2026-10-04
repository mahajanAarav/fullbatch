import { useEffect, useRef, useState } from 'react'
import ReactMarkdown from 'react-markdown'
import { ApiError, chatHistory, sendChat, type ChatLine } from '../api'

interface Props {
  role: 'seller' | 'buyer'
  intro: string
  suggestions: string[]
  onReply?: () => void // called after the assistant answers, so side panels can refresh
  syncKey?: number // change this to make the chat re-read its history (e.g. a payment hold landed)
}

// The assistant relays the PayPal approval link in its text. Show that one as a real button.
const isPayPalLink = (href?: string) => !!href && /(^|\/\/)([a-z0-9-]+\.)*paypal\.com\//i.test(href)

export function Chat({ role, intro, suggestions, onReply, syncKey = 0 }: Props) {
  const [lines, setLines] = useState<ChatLine[]>([])
  const [loaded, setLoaded] = useState(false)
  const [input, setInput] = useState('')
  const [sending, setSending] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const lastSent = useRef('')
  const sendingRef = useRef(false)
  const endRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    let cancelled = false
    chatHistory(role)
      // Never overwrite the screen while a message is in flight; the next sync will catch up.
      .then((history) => !cancelled && !sendingRef.current && setLines(history))
      .catch(() => undefined) // an empty chat is fine
      .finally(() => !cancelled && setLoaded(true))
    return () => {
      cancelled = true
    }
  }, [role, syncKey])

  useEffect(() => {
    endRef.current?.scrollIntoView({ behavior: 'smooth', block: 'end' })
  }, [lines, sending])

  async function send(text: string) {
    const message = text.trim()
    if (!message || sending) return
    lastSent.current = message
    setLines((prev) => [...prev, { role: 'user', text: message }])
    setInput('')
    setError(null)
    setSending(true)
    sendingRef.current = true
    try {
      const { reply } = await sendChat(role, message)
      setLines((prev) => [...prev, { role: 'assistant', text: reply }])
      onReply?.()
    } catch (e) {
      setError(
        e instanceof ApiError && e.status === 503
          ? 'The assistant is busy right now. Give it a moment, then try again.'
          : e instanceof Error
            ? e.message
            : 'Something went wrong.',
      )
    } finally {
      sendingRef.current = false
      setSending(false)
    }
  }

  return (
    <section className="chat">
      <div className="chat-log" aria-live="polite">
        {loaded && lines.length === 0 && (
          <div className="chat-empty">
            <p>{intro}</p>
            <div className="chips">
              {suggestions.map((s) => (
                <button key={s} className="chip" onClick={() => send(s)} disabled={sending}>
                  {s}
                </button>
              ))}
            </div>
          </div>
        )}

        {lines.map((line, i) => (
          <div key={i} className={`bubble bubble-${line.role}`}>
            {line.role === 'assistant' ? (
              <ReactMarkdown
                components={{
                  a: ({ href, children }) =>
                    isPayPalLink(href) ? (
                      <a className="paypal-button" href={href} target="_blank" rel="noreferrer">
                        Approve on PayPal →
                      </a>
                    ) : (
                      <a href={href} target="_blank" rel="noreferrer">
                        {children}
                      </a>
                    ),
                }}
              >
                {line.text}
              </ReactMarkdown>
            ) : (
              <p>{line.text}</p>
            )}
          </div>
        ))}

        {sending && (
          <div className="bubble bubble-assistant typing" aria-label="The assistant is typing">
            <span />
            <span />
            <span />
          </div>
        )}

        {error && (
          <div className="chat-error" role="alert">
            <span>{error}</span>
            <button className="link-button" onClick={() => send(lastSent.current)}>
              Try again
            </button>
          </div>
        )}
        <div ref={endRef} />
      </div>

      <form
        className="chat-input"
        onSubmit={(e) => {
          e.preventDefault()
          send(input)
        }}
      >
        <textarea
          value={input}
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === 'Enter' && !e.shiftKey) {
              e.preventDefault()
              send(input)
            }
          }}
          placeholder="Type a message…"
          rows={1}
          maxLength={2000}
          aria-label="Message"
        />
        <button className="button" type="submit" disabled={sending || !input.trim()}>
          Send
        </button>
      </form>
    </section>
  )
}
