import { expect, test } from 'claude-code/testing'

import { PANE, READY, openPane } from './pane-kit'
import type { Engine } from './pane-kit'

// readEnv runs inside the plugin (it needs the plugin's own `$`), so each test opens the pane with
// `connect_resolve.py` answering as named and reads what the home screen drew from it.
const buttonsOn = async ($: Engine): Promise<string[]> => {
  const ui = await $.ui.mount({ ...PANE, surface: 'desktop' })
  const labels = (await ui.findAll({ type: 'Button' })).map(b => b.text ?? '')
  await ui.unmount()

  return labels
}

test('the check runs connect_resolve.py once, through the plugin script wrapper', async ($, on) => {
  const argvs = await openPane($, on, { data: READY })

  expect(argvs).toHaveLength(1)
  expect(argvs[0]?.[0]).toBe('bash')
  expect(argvs[0]?.[1]).toMatch(/\/scripts\/py$/)
  expect(argvs[0]?.[2]).toBe('connect_resolve.py')
  // Drawing the pane again does not run it again.
  await buttonsOn($)
  await buttonsOn($)
  expect(argvs).toHaveLength(1)
})

test('a missing dependency is not a Python problem, so Connect still shows', async ($, on) => {
  await openPane($, on, { data: { ...READY, interpreter: { has_model_deps: false, chosen_because: 'no candidate has every dependency' } } })

  expect(await buttonsOn($)).toContain('Connect a database')
})

test('no interpreter at all hides Connect, and says to install Python', async ($, on) => {
  await openPane($, on, { data: { ...READY, interpreter: { chosen_because: 'no candidate interpreter found' } } })
  const ui = await $.ui.mount({ ...PANE, surface: 'desktop' })

  expect(await ui.find({ type: 'Text', text: /Install it from python.org/ })).toBeDefined()
  expect(await ui.find({ type: 'Button' })).toBeUndefined()
  await ui.unmount()
})

test('a script that fails to run is no usable Python, and nothing throws', async ($, on) => {
  await openPane($, on, { exitCode: 3, stderr: 'agami: no Python interpreter found', uname: 'Darwin' })

  expect(await buttonsOn($)).toEqual([])
})

test('a profile found by the script is listed, and its name heads the pane', async ($, on) => {
  await openPane($, on, { data: { ...READY, profile: 'acme', profiles: ['acme'] } })
  const ui = await $.ui.mount({ ...PANE, surface: 'terminal' })

  expect(await ui.find({ type: 'Text', text: /^acme$/ })).toBeDefined()
  await ui.unmount()
})
