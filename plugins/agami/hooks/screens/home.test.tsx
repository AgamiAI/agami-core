import { expect, test } from 'claude-code/testing'

import { PANE, READY, SURFACES, openPane } from '../pane-kit'
import { actions } from './home'

const WITH_PROFILES = { ...READY, profile: 'acme', profiles: ['acme', 'demo'] }

test('the actions are the ones the brief names, each with its exact prompt', () => {
  expect(actions.map(a => [a.key, a.label, a.prompt])).toEqual([
    ['connect', 'Connect a database', undefined],
    ['sample', 'Try the sample data', 'Run /agami-core:agami-connect sample and take the "Just let me query it" path.'],
    ['ask', 'Ask a question', 'I want to ask a question about my data with agami. Ask me what I want to know.'],
    ['review', 'Review the model', 'Run /agami-core:agami-model.'],
    ['reconcile', 'Check numbers against a dashboard', 'Run /agami-core:agami-reconcile. Ask me for the dashboard first.'],
    ['golden', 'Save and run test questions', 'Run /agami-core:agami-eval.'],
    ['reload', 'Reload a changed schema', 'Run /agami-core:agami-connect reintrospect for the active profile.'],
    ['serve', 'Use agami from other apps', 'Run /agami-core:agami-serve.'],
  ])
})

test('until the check lands the pane says it is checking', async $ => {
  for (const surface of SURFACES) {
    const ui = await $.ui.mount({ ...PANE, surface })

    expect(await ui.find({ type: 'Text', text: /Checking your setup/ })).toBeDefined()
    expect(await ui.find({ type: 'Button' })).toBeUndefined()
    await ui.unmount()
  }
})

test('with no profiles, Connect and the sample are all that is offered, in that order', async ($, on) => {
  await openPane($, on, { data: READY })

  for (const surface of SURFACES) {
    const ui = await $.ui.mount({ ...PANE, surface })

    expect((await ui.findAll({ type: 'Button' })).map(b => b.text)).toEqual(['Connect a database', 'Try the sample data'])
    expect(await ui.find({ type: 'Select' })).toBeUndefined()
    await ui.unmount()
  }
})

test('with a profile, the header names it, a Select lists it, and Review sends its prompt', async ($, on) => {
  const sent: string[] = []
  on('prompt.submit', (_, e) => {
    sent.push(e.text)

    return { text: e.text }
  })
  await openPane($, on, { data: WITH_PROFILES })

  for (const surface of SURFACES) {
    sent.length = 0
    const ui = await $.ui.mount({ ...PANE, surface })

    expect(await ui.find({ type: 'Text', text: /^acme$/ })).toBeDefined()
    expect(await ui.find({ type: 'Select' })).toBeDefined()
    expect((await ui.findAll({ type: 'Button' })).map(b => b.text)).toEqual(actions.map(a => a.label))

    await ui.press({ key: 'review' })
    expect(sent).toEqual(['Run /agami-core:agami-model.'])
    await ui.unmount()
  }
})

test('every skill action sends its prompt as given, and Connect sends none', async ($, on) => {
  const sent: string[] = []
  on('prompt.submit', (_, e) => {
    sent.push(e.text)

    return { text: e.text }
  })
  await openPane($, on, { data: WITH_PROFILES })
  const ui = await $.ui.mount({ ...PANE, surface: 'desktop' })

  // Connect last: it leaves the home screen.
  for (const a of [...actions.slice(1), actions[0]!]) await ui.press({ key: a.key })
  await ui.unmount()

  expect(sent).toEqual(actions.flatMap(a => (a.prompt === undefined ? [] : [a.prompt])))
})

test('the logo is an image where the surface has one, and text on the terminal', async ($, on) => {
  await openPane($, on, { data: READY })

  for (const surface of SURFACES) {
    const ui = await $.ui.mount({ ...PANE, surface })

    expect((await ui.find({ type: 'Svg' })) !== undefined).toBe(surface !== 'terminal')
    expect(await ui.find({ type: 'Text', text: /^agami$/ })).toBeDefined()
    await ui.unmount()
  }
})

test('with no usable Python the pane shows the fix instead of Connect and the sample', async ($, on) => {
  await openPane($, on, {
    data: { ...READY, interpreter: { chosen_because: 'every candidate was skipped; the first one' } },
    anomalies: [{ kind: 'agami_python_cannot_be_brought_current', detail: 'Install Python from python.org.' }],
  })

  for (const surface of SURFACES) {
    const ui = await $.ui.mount({ ...PANE, surface })

    expect(await ui.find({ type: 'Text', text: /Install Python from python.org/ })).toBeDefined()
    expect(await ui.find({ type: 'Button' })).toBeUndefined()
    await ui.unmount()
  }
})

test('Connect opens the Connect screen', async ($, on) => {
  await openPane($, on, { data: READY })
  const ui = await $.ui.mount({ ...PANE, surface: 'desktop' })

  await ui.press({ key: 'connect' })

  expect(await ui.find({ type: 'Input', key: 'profile' })).toBeDefined()
  expect(await ui.find({ type: 'Button', text: 'Connect a database' })).toBeUndefined()
  await ui.unmount()
})
