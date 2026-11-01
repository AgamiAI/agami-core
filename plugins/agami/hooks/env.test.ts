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

test('a script that finds no Python says to install one, and nothing throws', async ($, on) => {
  await openPane($, on, { exitCode: 3, stderr: 'agami: no Python interpreter found (tried $AGAMI_PYTHON, PATH).' })
  const ui = await $.ui.mount({ ...PANE, surface: 'desktop' })

  expect(await ui.find({ type: 'Text', text: /Install it from python.org/ })).toBeDefined()
  expect(await ui.find({ type: 'Button' })).toBeUndefined()
  await ui.unmount()
})

const textOn = async ($: Engine, pattern: RegExp): Promise<string | undefined> => {
  const ui = await $.ui.mount({ ...PANE, surface: 'desktop' })
  const found = await ui.find({ type: 'Text', text: pattern })
  await ui.unmount()

  return found?.text
}

test('a check that cannot start says so plainly, not that Python is missing', async ($, on) => {
  await openPane($, on, { spawnError: 'spawn bash ENOENT' })

  expect(await textOn($, /^Couldn't run agami's setup check: .*spawn bash ENOENT$/)).toBeDefined()
  expect(await textOn($, /python.org/)).toBeUndefined()
  expect(await buttonsOn($)).toEqual([])
})

test('a check that fails for another reason names its last line', async ($, on) => {
  await openPane($, on, { exitCode: 2, stderr: 'warming up\nconnect_resolve.py: error: bad flag\n' })

  expect(await textOn($, /^Couldn't run agami's setup check: connect_resolve.py: error: bad flag$/)).toBeDefined()
})

test('elsewhere than Windows the check runs through bash on the PATH', async ($, on) => {
  const argvs = await openPane($, on, { data: READY, env: { CLAUDE_CODE_GIT_BASH_PATH: 'C:\\Git\\bin\\bash.exe' } })

  expect(argvs.map(a => a[0])).toEqual(['bash'])
})

test('on Windows the check runs through CLAUDE_CODE_GIT_BASH_PATH when it is set', async ($, on) => {
  const pinned = 'D:\\Tools\\Git\\bin\\bash.exe'
  const argvs = await openPane($, on, {
    data: { ...READY, platform: 'Windows' },
    env: { OS: 'Windows_NT', CLAUDE_CODE_GIT_BASH_PATH: pinned },
  })

  expect(argvs).toHaveLength(1)
  expect(argvs[0]?.[0]).toBe(pinned)
  expect(argvs[0]?.[2]).toBe('connect_resolve.py')
})

test('on Windows the check runs through the bash.exe beside git.exe, never WSL’s', async ($, on) => {
  const gitBash = 'C:\\Program Files\\Git\\bin\\bash.exe'
  const argvs = await openPane($, on, {
    data: { ...READY, platform: 'Windows' },
    env: { OS: 'Windows_NT' },
    whereGit: 'C:\\Program Files\\Git\\cmd\\git.exe\r\n',
    files: [gitBash, 'C:\\Windows\\System32\\bash.exe'],
  })

  expect(argvs.map(a => a[0])).toEqual(['where', gitBash])
  expect(await buttonsOn($)).toContain('Connect a database')
})

test('on Windows with no Git Bash the pane says what to install, and runs no bash at all', async ($, on) => {
  const argvs = await openPane($, on, {
    data: { ...READY, platform: 'Windows' },
    env: { OS: 'Windows_NT' },
    files: ['C:\\Windows\\System32\\bash.exe'],
  })

  expect(argvs.filter(a => a[0] !== 'where')).toEqual([])
  expect(await textOn($, /^Couldn't run agami's setup check: .*Git for Windows/)).toBeDefined()
  expect(await buttonsOn($)).toEqual([])
})

test('a profile found by the script is listed, and its name heads the pane', async ($, on) => {
  await openPane($, on, { data: { ...READY, profile: 'acme', profiles: ['acme'] } })
  const ui = await $.ui.mount({ ...PANE, surface: 'terminal' })

  expect(await ui.find({ type: 'Text', text: /^acme$/ })).toBeDefined()
  await ui.unmount()
})
