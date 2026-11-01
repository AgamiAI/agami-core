import { expect, test } from 'claude-code/testing'

import { PANE, READY, SURFACES, openPane } from '../pane-kit'
import type { Engine } from '../pane-kit'
import { QUESTIONS } from './first-answer'

type Hooks = Parameters<typeof openPane>[1]

const SAMPLE = 'Run /agami-core:agami-connect sample and take the "Just let me query it" path.'
const MARKER = '/art/agami-example/datasource.yaml'
const NOTE = "The sample setup didn't finish. Check the chat to see where it stopped."

const SIX = [
  "What's our revenue by product category?",
  'Which product categories drive the most revenue in each sales channel?',
  'Who are the top 5 customers by total spend?',
  'Show the monthly revenue trend over the last 12 months.',
  'How many customers have both an order and a subscription?',
  'How many active subscriptions do we have, and what\'s MRR?',
]

type World = { sent: { text: string; asUser?: true }[]; checked: string[] }

async function openHome($: Engine, on: Hooks, exists: boolean): Promise<World> {
  const world: World = { sent: [], checked: [] }
  on('prompt.submit', (_, e) => {
    world.sent.push({ text: e.text, asUser: e.origin.kind === 'plugin' ? e.origin.asUser : undefined })

    return { text: e.text }
  })
  on('turn.complete', (_, e) => ({ text: e.answer }))
  on('fs.exists', (_, e) => {
    world.checked.push(e.path)

    return { value: exists }
  })
  await openPane($, on, { data: READY })

  return world
}

const finishTurn = ($: Engine) =>
  $.turn.complete({ answer: 'done', durationMs: 1, isAborted: false, turnId: 't1', reason: 'answer' })

test('First answer lists exactly the six sample questions, verbatim', () => {
  expect([...QUESTIONS]).toEqual(SIX)
})

test('pressing question 1 submits it as the user, and all six do', async ($, on) => {
  const world = await openHome($, on, true)

  for (const surface of SURFACES) {
    world.sent.length = 0
    const ui = await $.ui.mount({ ...PANE, surface })
    await ui.press({ key: 'sample' })
    await finishTurn($)

    expect((await ui.findAll({ type: 'Button' })).map(b => b.text)).toEqual([...SIX, 'Back to home'])
    await ui.press({ key: 'q1' })
    expect(world.sent.at(-1)).toEqual({ text: "What's our revenue by product category?", asUser: true })
    for (let i = 2; i <= 6; i++) await ui.press({ key: `q${i}` })
    expect(world.sent.slice(1).map(s => s.text)).toEqual(SIX)
    await ui.unmount()
    await $.command.run({ command: 'agami', args: '', origin: { kind: 'composer' }, presentation: { isFullscreen: true, columns: 160 } })
  }
})

test('home switches to First answer only when the sample turn ends with datasource.yaml present', async ($, on) => {
  const world = await openHome($, on, true)
  const ui = await $.ui.mount({ ...PANE, surface: 'desktop' })

  await ui.press({ key: 'sample' })
  expect(world.sent[0]).toEqual({ text: SAMPLE, asUser: undefined })
  expect(await ui.find({ type: 'Button', key: 'q1' })).toBeUndefined()
  await finishTurn($)

  expect(world.checked).toEqual([MARKER])
  expect(await ui.find({ type: 'Button', key: 'q1' })).toBeDefined()
  await ui.unmount()
})

test('without the file, the pane stays home with a one-line note', async ($, on) => {
  await openHome($, on, false)
  const ui = await $.ui.mount({ ...PANE, surface: 'desktop' })

  await ui.press({ key: 'sample' })
  await finishTurn($)

  expect(await ui.find({ type: 'Button', key: 'q1' })).toBeUndefined()
  expect(await ui.find({ type: 'Text', text: NOTE })).toBeDefined()
  expect(await ui.find({ type: 'Button', key: 'sample' })).toBeDefined()
  await ui.unmount()
})

test('a turn the sample did not start never moves the pane', async ($, on) => {
  const world = await openHome($, on, true)
  const ui = await $.ui.mount({ ...PANE, surface: 'desktop' })

  await finishTurn($)

  expect(world.checked).toEqual([])
  expect(await ui.find({ type: 'Button', key: 'q1' })).toBeUndefined()
  expect(await ui.find({ type: 'Text', text: NOTE })).toBeUndefined()
  await ui.unmount()
})

test('Back to home returns to the home screen', async ($, on) => {
  await openHome($, on, true)
  const ui = await $.ui.mount({ ...PANE, surface: 'terminal' })

  await ui.press({ key: 'sample' })
  await finishTurn($)
  await ui.press({ key: 'home' })

  expect(await ui.find({ type: 'Button', key: 'sample' })).toBeDefined()
  expect(await ui.find({ type: 'Button', key: 'q1' })).toBeUndefined()
  await ui.unmount()
})
