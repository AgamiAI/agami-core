import { atom, update } from 'claude-code'
import type { CoreEngineInterface, RenderElement, RenderInput } from 'claude-code'

import type { AgamiScreen } from '../../types'
import { BRAND } from '../brand'

// Declared in each file that reads or writes them: the engine lists the values a module touches by
// reading the atom where it is used, so an imported atom would not be seen.
const screenState = atom({ plugin: 'agami-core', key: 'screen' } as const, 'home' as AgamiScreen)

type Engine = Pick<CoreEngineInterface, 'ui' | 'state' | 'prompt'>

// The starter questions agami-connect's sample path (Phase 0s, step 7) offers, without its join hints.
// All six are backed by validated seed examples in the shipped sample model.
export const QUESTIONS = [
  "What's our revenue by product category?",
  'Which product categories drive the most revenue in each sales channel?',
  'Who are the top 5 customers by total spend?',
  'Show the monthly revenue trend over the last 12 months.',
  'How many customers have both an order and a subscription?',
  "How many active subscriptions do we have, and what's MRR?",
] as const

/** Asks the question as the person's own message, so Claude answers it in the chat. */
export async function ask($: Pick<Engine, 'prompt'>, question: string): Promise<void> {
  await $.prompt.submit({ text: question, asUser: true })
}

export async function firstAnswer($: Engine, e: RenderInput): Promise<RenderElement> {
  const { Box, Text, Button } = $.ui.resolve(e)

  return (
    <Box flexDirection="column">
      <Text bold color={BRAND}>
        agami
      </Text>
      <Text>The sample data is ready. Pick a question and the answer appears in the chat.</Text>
      <Box flexDirection="column" marginTop={1}>
        {QUESTIONS.map((q, i) => (
          <Button key={`q${i + 1}`} label={q} onPress={() => ask($, q)} />
        ))}
      </Box>
      <Box marginTop={1}>
        <Button key="home" label="Back to home" onPress={() => update($, screenState, () => 'home')} />
      </Box>
    </Box>
  )
}
