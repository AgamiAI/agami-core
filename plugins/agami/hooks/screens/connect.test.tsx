import { expect, mock, test } from 'claude-code/testing'

import { PANE, READY, SURFACES, openPane } from '../pane-kit'
import type { Engine, Resolve } from '../pane-kit'

type Hooks = Parameters<typeof openPane>[1]

const URL = 'http://localhost:50123/tok/'
const SPEC_PATH = '/art/local/tmp/connect-demo.json'
const LINK_NOTE = 'Opens a page on this computer. The password goes to your keychain, not the chat.'
const CONTINUE = 'The agami profile demo is connected. Continue /agami-core:agami-connect from Phase 1.3 for it (skip Phase 0).'

const line = (obj: object): string => `${JSON.stringify(obj)}\n`

type World = {
  argvs: string[][]
  spawned: string[][]
  writes: { path: string; text: string }[]
  sent: string[]
  stopped: { value: boolean }
}

/**
 * Opens the pane and the Connect screen with the helper answering `lines`; a number in `lines` is a
 * pause on the mocked clock, so the test can look at the pane while the helper waits.
 */
async function openConnect(
  $: Engine,
  on: Hooks,
  lines: (string | number)[],
  resolve: Resolve = { data: READY },
  options: { killDelay?: number; drops?: number } = {},
): Promise<World & { clock: ReturnType<typeof mock.clock> }> {
  const clock = mock.clock(on)
  const world: World = { argvs: [], spawned: [], writes: [], sent: [], stopped: { value: false } }
  on('fs.write', (_, e) => {
    world.writes.push({ path: e.path, text: e.text })

    return { value: undefined }
  })
  let drops = options.drops ?? 0
  on('prompt.submit', (_, e) => {
    if (drops > 0) {
      drops -= 1

      return { drop: 'A turn is running.' }
    }
    world.sent.push(e.text)

    return { text: e.text }
  })
  on('process.spawn', async function* (_, e, next) {
    world.spawned.push([...e.argv])
    // The engine aborts the signal when the caller ends the stream: the child is killed then.
    const killed = new Promise<'killed'>(resolve => next.signal.addEventListener('abort', () => resolve('killed')))
    try {
      for (const piece of lines) {
        if (typeof piece !== 'number') yield { stream: 'stdout' as const, text: piece }
        else if ((await Promise.race([clock.sleep(piece), killed])) === 'killed') {
          // A child can take a moment to go once signalled; its run's cleanup comes after that.
          if (options.killDelay) await clock.sleep(options.killDelay)

          return { value: { code: null, signal: 'SIGTERM' } }
        }
      }
    } finally {
      world.stopped.value = true
    }

    return { value: { code: 0, signal: null } }
  })
  world.argvs = await openPane($, on, resolve)

  return { ...world, clock }
}

async function fillForm(
  ui: { input: (t: { key: string; text: string; kind?: 'change' | 'submit' }) => Promise<unknown> },
  profile = 'demo',
): Promise<void> {
  await ui.input({ key: 'profile', text: profile, kind: 'change' })
  await ui.input({ key: 'host', text: 'your-host.example.com', kind: 'change' })
  await ui.input({ key: 'database', text: 'SALES_DATA', kind: 'change' })
  await ui.input({ key: 'user', text: 'analyst', kind: 'change' })
}

test('a profile name that breaks the rule shows why, and starts nothing', async ($, on) => {
  const w = await openConnect($, on, [])
  const ui = await $.ui.mount({ ...PANE, surface: 'desktop' })
  await ui.press({ key: 'connect' })

  await fillForm(ui, 'Bad Name!')
  await ui.press({ key: 'start' })

  expect(await ui.find({ type: 'Text', text: /1 to 32 lowercase letters/ })).toBeDefined()
  expect(w.spawned).toEqual([])
  expect(w.writes).toEqual([])
  await ui.unmount()
})

test('a profile name already in use shows why, and starts nothing', async ($, on) => {
  const w = await openConnect($, on, [], { data: { ...READY, profile: 'demo', profiles: ['demo'] } })
  const ui = await $.ui.mount({ ...PANE, surface: 'desktop' })
  await ui.press({ key: 'connect' })

  await fillForm(ui, 'demo')
  await ui.press({ key: 'start' })

  expect(await ui.find({ type: 'Text', text: /already a profile named demo/ })).toBeDefined()
  expect(w.spawned).toEqual([])
  await ui.unmount()
})

// A press that leaves the helper waiting on the mocked clock takes about a second to settle, three
// surfaces over.
test('on a Mac the helper starts from a spec file and its page shows as a Link', { timeoutMs: 15_000 }, async ($, on) => {
  const w = await openConnect($, on, [line({ url: URL, expires_in: 300 }), 300_000])

  for (const surface of SURFACES) {
    w.writes.length = 0
    w.spawned.length = 0
    const ui = await $.ui.mount({ ...PANE, surface })
    await ui.press({ key: 'connect' })
    await fillForm(ui)
    await ui.press({ key: 'start' })

    expect(w.writes.map(x => x.path)).toEqual([SPEC_PATH])
    expect(JSON.parse(w.writes[0]!.text)).toEqual({
      profile: 'demo',
      db_type: 'postgres',
      fields: { host: 'your-host.example.com', port: '5432', database: 'SALES_DATA', user: 'analyst' },
    })
    expect(w.spawned).toHaveLength(1)
    expect(w.spawned[0]?.slice(2)).toEqual(['connect_helper.py', '--spec', SPEC_PATH])
    expect(w.spawned[0]?.[1]).toMatch(/\/scripts\/py$/)
    expect(await ui.find({ type: 'Text', text: LINK_NOTE })).toBeDefined()
    expect((await ui.find({ type: 'Link' }))?.props).toEqual({ href: URL, label: 'Enter the password' })
    // A drawing unmounts once the work under way settles, and the helper waits until stopped.
    await ui.press({ key: 'back' })
    await ui.unmount()
  }
})

test('a connected helper sends the continue prompt, goes home, and reads the setup again', async ($, on) => {
  const w = await openConnect($, on, [line({ url: URL, expires_in: 300 }), 1000, line({ status: 'connected', profile: 'demo', detail: 'ok' })])
  const ui = await $.ui.mount({ ...PANE, surface: 'desktop' })
  await ui.press({ key: 'connect' })
  await fillForm(ui)
  await ui.press({ key: 'start' })
  expect(w.sent).toEqual([])

  await w.clock.advance(1000)

  expect(w.sent).toEqual([CONTINUE])
  expect(await ui.find({ type: 'Button', text: 'Connect a database' })).toBeDefined()
  expect(w.argvs.filter(a => a[2] === 'connect_resolve.py')).toHaveLength(2)
  // The spec file goes once the helper has said how it ended.
  expect(w.argvs.some(a => a.includes(SPEC_PATH))).toBe(true)
  await ui.unmount()
})

test('a failed connection shows the reason and Try again returns to the form', async ($, on) => {
  const w = await openConnect($, on, [
    line({ url: URL, expires_in: 300 }),
    line({ status: 'connect_failed', profile: 'demo', detail: 'The server refused the sign-in.' }),
  ])
  const ui = await $.ui.mount({ ...PANE, surface: 'desktop' })
  await ui.press({ key: 'connect' })
  await fillForm(ui)
  await ui.press({ key: 'start' })

  expect(await ui.find({ type: 'Text', text: /The server refused the sign-in/ })).toBeDefined()
  expect(w.sent).toEqual([])
  await ui.press({ key: 'retry' })
  expect(await ui.find({ type: 'Input', key: 'profile' })).toBeDefined()
  await ui.unmount()
})

test('a helper that ends without a final line is a failure, with Try again', async ($, on) => {
  const w = await openConnect($, on, ['agami: a stray warning\n'])
  const ui = await $.ui.mount({ ...PANE, surface: 'desktop' })
  await ui.press({ key: 'connect' })
  await fillForm(ui)
  await ui.press({ key: 'start' })

  expect(await ui.find({ type: 'Text', text: /stopped without saying how it ended/ })).toBeDefined()
  expect(await ui.find({ type: 'Button', text: 'Try again' })).toBeDefined()
  expect(w.argvs.some(a => a.includes(SPEC_PATH))).toBe(true)
  await ui.unmount()
})

test('an expired page goes back to the form and says so', async ($, on) => {
  const w = await openConnect($, on, [line({ url: URL, expires_in: 300 }), line({ status: 'expired' })])
  const ui = await $.ui.mount({ ...PANE, surface: 'desktop' })
  await ui.press({ key: 'connect' })
  await fillForm(ui)
  await ui.press({ key: 'start' })

  expect(await ui.find({ type: 'Input', key: 'profile' })).toBeDefined()
  expect(await ui.find({ type: 'Text', text: /expired/ })).toBeDefined()
  expect(w.sent).toEqual([])
  await ui.unmount()
})

test('Snowflake with browser sign-in sends auth sso and shows no password link', async ($, on) => {
  const w = await openConnect($, on, [1000, line({ status: 'connected', profile: 'demo', detail: 'ok' })])
  const ui = await $.ui.mount({ ...PANE, surface: 'desktop' })
  await ui.press({ key: 'connect' })
  await ui.select({ key: 'dbType', value: 'snowflake' })
  await ui.select({ key: 'auth', value: 'sso' })
  await ui.input({ key: 'profile', text: 'demo', kind: 'change' })
  await ui.input({ key: 'account', text: 'xy12345.us-east-1', kind: 'change' })
  await ui.input({ key: 'user', text: 'you@example.com', kind: 'change' })
  await ui.press({ key: 'start' })

  expect(JSON.parse(w.writes[0]!.text)).toEqual({
    profile: 'demo',
    db_type: 'snowflake',
    auth: 'sso',
    fields: { account: 'xy12345.us-east-1', user: 'you@example.com', authenticator: 'externalbrowser' },
  })
  expect(await ui.find({ type: 'Link' })).toBeUndefined()
  await w.clock.advance(1000)
  expect(w.sent).toEqual([CONTINUE])
  await ui.unmount()
})

test('Back while the helper waits stops it and goes home', async ($, on) => {
  const w = await openConnect($, on, [line({ url: URL, expires_in: 300 }), 300_000])
  const ui = await $.ui.mount({ ...PANE, surface: 'desktop' })
  await ui.press({ key: 'connect' })
  await fillForm(ui)
  await ui.press({ key: 'start' })
  expect(w.stopped.value).toBe(false)

  await ui.press({ key: 'back' })

  expect(w.stopped.value).toBe(true)
  expect(await ui.find({ type: 'Button', text: 'Connect a database' })).toBeDefined()
  expect(w.argvs.some(a => a.includes(SPEC_PATH))).toBe(true)
  await ui.unmount()
})

test('opening the pane again stops a waiting helper and starts at home', async ($, on) => {
  const w = await openConnect($, on, [line({ url: URL, expires_in: 300 }), 300_000])
  const ui = await $.ui.mount({ ...PANE, surface: 'desktop' })
  await ui.press({ key: 'connect' })
  await fillForm(ui)
  await ui.press({ key: 'start' })

  await $.command.run({
    command: 'agami',
    args: '',
    origin: { kind: 'composer' },
    presentation: { isFullscreen: true, columns: 160 },
  })

  expect(w.stopped.value).toBe(true)
  expect(await ui.find({ type: 'Button', text: 'Connect a database' })).toBeDefined()
  await ui.unmount()
})

test('elsewhere the template is Claude’s to write, and Check again promotes it', async ($, on) => {
  let promote = 'PLACEHOLDERS_REMAIN your-password'
  const w = await openConnect($, on, [], {
    data: { ...READY, platform: 'Linux' },
    scripts: { 'promote_credentials.py': () => `${promote}\n` },
  })
  const ui = await $.ui.mount({ ...PANE, surface: 'desktop' })
  await ui.press({ key: 'connect' })
  await fillForm(ui)
  await ui.press({ key: 'start' })

  expect(w.spawned).toEqual([])
  expect(w.sent).toHaveLength(1)
  expect(w.sent[0]).toMatch(/Phase 0a\.4/)
  expect(w.sent[0]).toMatch(/postgres/)
  expect(w.sent[0]).toMatch(/demo/)
  expect(await ui.find({ type: 'Text', text: '/art/local/credentials.example' })).toBeDefined()
  expect(await ui.find({ type: 'Button', text: 'Check again' })).toBeDefined()

  await ui.press({ key: 'check' })
  expect(await ui.find({ type: 'Text', text: /still has placeholder values/ })).toBeDefined()
  expect(w.sent).toHaveLength(1)

  promote = 'SECURED demo'
  await ui.press({ key: 'check' })
  expect(w.sent).toEqual([w.sent[0], CONTINUE])
  expect(await ui.find({ type: 'Button', text: 'Connect a database' })).toBeDefined()
  await ui.unmount()
})

const removals = (argvs: string[][]): number => argvs.filter(a => a[1] === '-c' && a.includes(SPEC_PATH)).length

// Each press that leaves a helper asleep on the mocked clock takes about a second to settle.
test('a stopped helper’s late cleanup leaves the spec of the run that replaced it', { timeoutMs: 15_000 }, async ($, on) => {
  const w = await openConnect($, on, [line({ url: URL, expires_in: 300 }), 300_000], { data: READY }, { killDelay: 50 })
  const ui = await $.ui.mount({ ...PANE, surface: 'desktop' })
  await ui.press({ key: 'connect' })
  await fillForm(ui)
  await ui.press({ key: 'start' })
  await ui.press({ key: 'back' })
  // The same profile again, before the first helper has gone.
  await ui.press({ key: 'connect' })
  await fillForm(ui)
  await ui.press({ key: 'start' })
  expect(w.writes.map(x => x.path)).toEqual([SPEC_PATH, SPEC_PATH])

  await w.clock.advance(50)
  expect(removals(w.argvs)).toBe(0)

  await ui.press({ key: 'back' })
  await w.clock.advance(50)
  expect(removals(w.argvs)).toBe(1)
  await ui.unmount()
})

test('Check again continues with the profile the script promoted', async ($, on) => {
  const w = await openConnect($, on, [], {
    data: { ...READY, platform: 'Linux' },
    scripts: { 'promote_credentials.py': 'APPENDED analytics\n' },
  })
  const ui = await $.ui.mount({ ...PANE, surface: 'desktop' })
  await ui.press({ key: 'connect' })
  await fillForm(ui)
  await ui.press({ key: 'start' })

  await ui.press({ key: 'check' })

  expect(w.sent.slice(1)).toEqual([CONTINUE.replace('demo', 'analytics')])
  await ui.unmount()
})

test('a connected profile whose hand-off fails says so, and Try again sends it', async ($, on) => {
  const w = await openConnect($, on, [line({ status: 'connected', profile: 'demo', detail: 'ok' })], { data: READY }, { drops: 1 })
  const ui = await $.ui.mount({ ...PANE, surface: 'desktop' })
  await ui.press({ key: 'connect' })
  await fillForm(ui)
  await ui.press({ key: 'start' })

  expect(await ui.find({ type: 'Text', text: 'demo is connected, but the next step could not start.' })).toBeDefined()
  expect(await ui.find({ type: 'Text', text: /helper/ })).toBeUndefined()
  expect(w.sent).toEqual([])

  await ui.press({ key: 'continue' })
  expect(w.sent).toEqual([CONTINUE])
  expect(await ui.find({ type: 'Button', text: 'Connect a database' })).toBeDefined()
  await ui.unmount()
})

test('no field on the Connect screen is a password field, on any surface or database', async ($, on) => {
  await openConnect($, on, [])

  for (const surface of SURFACES) {
    const ui = await $.ui.mount({ ...PANE, surface })
    if ((await ui.find({ type: 'Button', text: 'Connect a database' })) !== undefined) await ui.press({ key: 'connect' })
    for (const db of ['postgres', 'mysql', 'snowflake', 'sqlserver', 'other']) {
      await ui.select({ key: 'dbType', value: db })
      const inputs = await ui.findAll({ type: 'Input' })
      if (db !== 'other') expect(inputs.length).toBeGreaterThan(1)
      for (const input of inputs) {
        const said = [input.key, input.props.label, input.props.placeholder].join(' ')
        expect(said.toLowerCase()).not.toContain('password')
      }
    }
    await ui.unmount()
  }
})
