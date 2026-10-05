import { createAiHarness, directLlmRunner } from 'ag-studio'
import type { AgAiAgentContext, AgAiHarnessSetup, AgAiModel, AgAiPromptStarter } from 'ag-studio'
import { myPlan } from '../api'
import { fullbatchAdapter } from './adapter'

// The assistant inside the Dashboard: AG Studio's Agent Framework with a custom "Drop planner" agent.
// It reads the dashboard's data with Studio's own tools, gets recommendations from our planner
// (numbers computed by code, not by the model), and can open the New drop form pre-filled.
// It never creates a drop itself: the seller reviews and confirms.

export const PREPARE_DROP_EVENT = 'fullbatch:prepare-drop'

const INSTRUCTIONS = `You are the Drop planner inside a seller's dashboard on fullbatch, a preorder platform where buyers' PayPal payments are only HELD, and are charged only if a drop reaches its minimum by the deadline.

How to work:
- For "what should I run next?", "how did my last drop go?" and any planning question, call recommend_next_drop. It returns figures computed from the seller's own finished drops. Explain them in plain, friendly language and say how confident the result is. Use ONLY the figures and the reasons the tool returned: do not change a figure, and do not add reasoning of your own (about timing, weekdays, price or demand) that the tool did not give. If the seller asks for something the tool does not provide, say so.
- For questions about the dashboard's own numbers (sales, orders, which drop did best), look at the schema and run a query with the data tools instead of guessing.
- If the seller wants to set up the suggested drop, call prepare_drop with the recommended values. That only opens a form for them to review. You cannot create or cancel drops, and you must not claim to have.
- Keep answers short. Money is in US dollars.`

const MODELS: AgAiModel[] = [{ id: 'fullbatch', label: 'fullbatch assistant' }]

const PROMPT_STARTERS: AgAiPromptStarter[] = [
  { label: 'How did my last drop go?', prompt: 'How did my last drop go?' },
  { label: 'What should I run next?', prompt: 'What should I run next, and why?' },
  { label: 'Which drop sold best?', prompt: 'Which of my drops has sold the most so far?' },
]

export const dashboardAi: AgAiHarnessSetup =
  ({ api }) =>
    createAiHarness(api, () => ({
      primary: 'drop-planner',
      models: MODELS,
      promptStarters: PROMPT_STARTERS,
      agents: [
        directLlmRunner({
          id: 'drop-planner',
          name: 'Drop planner',
          description: 'Reviews finished drops and recommends the next one.',
          adapter: fullbatchAdapter,
          instructions: () => INSTRUCTIONS,
          tools: ({ api: studio, studio: tools }: AgAiAgentContext) => [
            tools.viewSchema(),
            tools.executeQuery(),
            studio.defineAiTool({
              name: 'recommend_next_drop',
              description:
                "Reviews the seller's finished drops and recommends quantity, minimum, price and timing for the next one. Read-only.",
              params: (s) => s.object({}),
              execute: async (_args, ctx) => {
                try {
                  const { recommendation, reports } = await myPlan()
                  return ctx.success(JSON.stringify({ recommendation, last_finished_drop: reports[0] ?? null }))
                } catch (e) {
                  return ctx.error(e instanceof Error ? e.message : 'Could not load the plan.')
                }
              },
            }),
            studio.defineAiTool({
              name: 'prepare_drop',
              description:
                'Opens the New drop form pre-filled with these values so the seller can review and confirm. It does NOT create the drop.',
              params: (s) =>
                s.object({
                  item_name: s.string({ description: 'What is being sold' }),
                  unit_price: s.number({ description: 'Price per unit in US dollars' }),
                  quantity_total: s.number({ description: 'Total units' }),
                  minimum_units: s.number({ description: 'Units needed for the drop to run' }),
                  max_per_buyer: s.number({ description: 'Most units one buyer can order' }),
                  deadline: s.string({ description: 'When ordering closes, ISO 8601 with a timezone' }),
                }),
              execute: async (args, ctx) => {
                window.dispatchEvent(new CustomEvent(PREPARE_DROP_EVENT, { detail: args }))
                return ctx.success('Opened the New drop form with these values for the seller to review. Nothing has been created yet.')
              },
            }),
          ],
        }),
      ],
    }))
