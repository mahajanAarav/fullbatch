import { useEffect, useRef, useState } from 'react'
import ReactMarkdown from 'react-markdown'
import { ApiError, chatHistory, sendBuyerChat, sendSellerChat, type ChatLine } from '../api'

interface Props {
  role: 'seller' | 'buyer'
  sessionId: string
  sellerId?: number
  intro: string
  suggestions: string[]
  onReply?: () => void // called after the assistant answers, so side panels can refresh
}

// The assistant relays the PayPal approval link in its text. Show that one as a real button.
const isPayPalLink = (href?: string) => !!href && /(^|\/\/)([a-z0-9-]+\.)*paypal\.com\//i.test(href)

export function Chat({ role, sessionId, sellerId, intro, suggestions, onReply }: Props) {
  const [lines, setLines] = useState<ChatLine[]>([])
  const [loaded, setLoaded] = useState(false)
  const [input, setInput] = useState('')
  const [sending, setSending] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const lastSent = useRef('')
  const endRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    let cancelled = false
    chatHistory(role, sessionId)
      .then((history) => !cancelled && setLines(history))
      .catch(() => undefined) // an empty chat is fine
      .finally(() => !cancelled && setLoaded(true))
    return () => {
      cancelled = true
    }
  }, [role, sessionId])

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
    try {
      const { reply } =
        role === 'seller' && sellerId !== undefined
          ? await sendSellerChat(sellerId, sessionId, message)
          : await sendBuyerChat(sessionId, message)
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
      setSending(false)
    }
  }

  return (
    <section className="chat card">
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
