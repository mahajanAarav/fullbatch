import type {
  AgAiConversationItem,
  AgAiEvent,
  AgAiOutputItem,
  AgLlmAdapter,
  AgLlmRequest,
  AgLlmResponse,
} from 'ag-studio'

// AG Studio ships no model adapters. This one sends each assistant turn to OUR server (/api/ai/turn),
// which runs it through the Groq-then-Gemini chain. No API key ever reaches the browser.

interface WireCall {
  id: string
  name: string
  args: Record<string, unknown>
}
interface WireMessage {
  role: 'user' | 'assistant' | 'tool'
  text?: string
  tool_calls?: WireCall[]
  call_id?: string
  name?: string
  result?: string
}
interface WireReply {
  text: string | null
  tool_calls: WireCall[]
}

function parseArgs(text: string): Record<string, unknown> {
  try {
    const value = JSON.parse(text || '{}')
    return value && typeof value === 'object' && !Array.isArray(value) ? value : {}
  } catch {
    return {}
  }
}

// Studio's conversation (messages, function calls, call outputs) -> our plain message list.
export function toWire(request: AgLlmRequest) {
  let instructions = request.instructions ?? ''
  const messages: WireMessage[] = []
  const callNames = new Map<string, string>()
  let assistant: WireMessage | null = null
  const flush = () => {
    if (assistant) messages.push(assistant)
    assistant = null
  }

  for (const item of request.input as AgAiConversationItem[]) {
    if (item.type === 'message' && item.kind === 'input') {
      flush()
      const text = item.content.map((c) => (c.type === 'text' ? c.text : '')).join('')
      if (item.role === 'system') instructions += `\n\n${text}`
      else messages.push({ role: 'user', text })
    } else if (item.type === 'message') {
      flush()
      const text = item.content.map((c) => (c.type === 'text' ? c.text : c.refusal)).join('')
      assistant = { role: 'assistant', text, tool_calls: [] }
    } else if (item.type === 'function_call') {
      assistant ??= { role: 'assistant', tool_calls: [] }
      callNames.set(item.callId, item.name)
      assistant.tool_calls!.push({ id: item.callId, name: item.name, args: parseArgs(item.arguments) })
    } else if (item.type === 'function_call_output') {
      flush()
      messages.push({ role: 'tool', call_id: item.callId, name: callNames.get(item.callId) ?? 'tool', result: item.output })
    }
    // Reasoning items are the model's private notes: not sent back.
  }
  flush()

  if (request.responseFormat.type === 'json') {
    instructions += `\n\nRespond with ONLY valid JSON matching this JSON Schema, with no other text: ${JSON.stringify(request.responseFormat.schema)}`
  }
  const choice = request.toolChoice
  if (choice && typeof choice === 'object') instructions += `\n\nYou must call the tool "${choice.name}" now.`

  const tools = choice === 'none' ? [] : (request.tools ?? []).map((t) => ({ name: t.name, description: t.description, parameters: t.parameters }))
  return { instructions: instructions.trim(), messages, tools }
}

const uid = () => crypto.randomUUID()

function toOutput(reply: WireReply): AgAiOutputItem[] {
  const output: AgAiOutputItem[] = []
  if (reply.text) {
    output.push({ id: uid(), kind: 'output', type: 'message', role: 'assistant', status: 'completed', content: [{ type: 'text', text: reply.text, annotations: [] }] })
  }
  for (const call of reply.tool_calls) {
    output.push({ id: uid(), kind: 'output', type: 'function_call', callId: call.id, name: call.name, arguments: JSON.stringify(call.args ?? {}), status: 'completed' })
  }
  return output
}

function toEvents(output: AgAiOutputItem[]): AgAiEvent[] {
  const events: AgAiEvent[] = []
  for (const item of output) {
    if (item.type === 'message') {
      const text = item.content.map((c) => (c.type === 'text' ? c.text : c.refusal)).join('')
      events.push({ type: 'TEXT_MESSAGE_START', messageId: item.id, role: 'assistant' } as AgAiEvent)
      events.push({ type: 'TEXT_MESSAGE_CONTENT', messageId: item.id, delta: text })
      events.push({ type: 'TEXT_MESSAGE_END', messageId: item.id })
    } else if (item.type === 'function_call') {
      events.push({ type: 'TOOL_CALL_START', toolCallId: item.callId, toolCallName: item.name })
      events.push({ type: 'TOOL_CALL_ARGS', toolCallId: item.callId, delta: item.arguments })
      events.push({ type: 'TOOL_CALL_END', toolCallId: item.callId })
    }
  }
  return events
}

async function runTurn(request: AgLlmRequest, signal?: AbortSignal): Promise<{ response: AgLlmResponse; events: AgAiEvent[] }> {
  const id = uid()
  const failed = (message: string, code: string) => ({
    response: { id, createdAt: Date.now(), status: 'failed', output: [], error: { code, message } } as AgLlmResponse,
    events: [{ type: 'RUN_ERROR', message, code } as AgAiEvent],
  })
  try {
    const res = await fetch('/api/ai/turn', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(toWire(request)),
      signal,
    })
    if (!res.ok) {
      let message = `The assistant is unavailable (${res.status}).`
      try {
        const body = await res.json()
        message = body.error ?? body.detail ?? message
      } catch {
        /* keep the generic message */
      }
      return failed(message, `http_${res.status}`)
    }
    const reply = (await res.json()) as WireReply
    const output = toOutput(reply)
    return { response: { id, createdAt: Date.now(), status: 'completed', output, model: 'fullbatch' }, events: toEvents(output) }
  } catch (e) {
    if (signal?.aborted) return { response: { id, createdAt: Date.now(), status: 'cancelled', output: [] }, events: [] }
    return failed(e instanceof Error ? e.message : 'Could not reach the assistant.', 'network')
  }
}

export const fullbatchAdapter: AgLlmAdapter = {
  executeTurn(request, options) {
    const turn = runTurn(request, options?.signal) // one request, shared by the stream and the final response
    return {
      stream: {
        async *[Symbol.asyncIterator]() {
          for (const event of (await turn).events) yield event
        },
      },
      complete: turn.then((t) => t.response),
    }
  },
}
