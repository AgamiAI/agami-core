import { expect, mock, test } from 'claude-code/testing'

import { PANE, READY, SURFACES, openPane } from '../pane-kit'
import type { Engine, Resolve } from '../pane-kit'

type Hooks = Parameters<typeof openPane>[1]

const URL = 'http://localhost:50123/tok/'
const SPEC_PATH = '/art/local/tmp/connect-demo.json'
const LINK_NOTE = 'Opens a page on this computer. The password goes to your keychain, not the chat.'
const CONTINUE =
  'The agami profile demo is connected and its credentials are secured. Continue /agami-core:agami-connect for it: ' +
  'run Phase 0a.5 to 0a.8 (skip the credential template and promotion), then Phase 1.'

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
  options: { killDelay?: number; drops?: number; then?: (string | number)[] } = {},
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
    // A helper started again (after an install) answers `then`, when the test names it.
    const answer = world.spawned.length > 1 && options.then !== undefined ? options.then : lines
    try {
      for (const piece of answer) {
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


// --- The helper's refusals and missing driver: fixed in the pane, then the helper runs again. ---

const NO_STORE = line({
  status: 'refused',
  reason: 'no_store',
  profile: 'demo',
  detail: 'The secure store isn’t set up for agami yet: your Keychain needs the keyring library, which sm install adds.',
})
const CONNECTED = line({ status: 'connected', profile: 'demo', detail: 'ok' })

const isSmInstall = (argv: string[]): boolean => /\/scripts\/sm$/.test(argv[1] ?? '') && argv[2] === 'install'
const pipArgs = (argv: string[]): string[] | undefined =>
  /\/scripts\/py$/.test(argv[1] ?? '') && argv[2] === '-m' && argv[3] === 'pip' ? argv.slice(2) : undefined

// The return type is inferred: the mounted Pane is narrower than what `ui.mount` declares in general.
async function pressConnect($: Engine) {
  const ui = await $.ui.mount({ ...PANE, surface: 'desktop' })
  await ui.press({ key: 'connect' })
  await fillForm(ui)
  await ui.press({ key: 'start' })

  return ui
}

test('no secure store offers to set it up, and installs nothing before the press', async ($, on) => {
  const w = await openConnect($, on, [NO_STORE])
  const ui = await pressConnect($)

  expect(await ui.find({ type: 'Text', text: 'The secure store isn’t set up for agami yet.' })).toBeDefined()
  expect((await ui.find({ type: 'Button', key: 'setup-store' }))?.text).toBe('Set up secure storage')
  expect(w.argvs.filter(isSmInstall)).toEqual([])
  await ui.unmount()
})

test('Set up secure storage runs sm install, then starts the helper again', async ($, on) => {
  const w = await openConnect($, on, [NO_STORE], { data: READY }, { then: [line({ url: URL, expires_in: 300 }), CONNECTED] })
  const ui = await pressConnect($)

  await ui.press({ key: 'setup-store' })

  const installs = w.argvs.filter(isSmInstall)
  expect(installs).toHaveLength(1)
  expect(installs[0]?.[0]).toBe('bash')
  expect(w.spawned).toHaveLength(2)
  expect(w.spawned[1]?.slice(2)).toEqual(['connect_helper.py', '--spec', SPEC_PATH])
  expect(w.sent).toEqual([CONTINUE])
  await ui.unmount()
})

test('a failed secure-store setup moves to the file you edit, and says why in one line', async ($, on) => {
  const w = await openConnect($, on, [NO_STORE], {
    data: READY,
    run: argv => (isSmInstall(argv) ? { exitCode: 3, stderr: 'agami: installing\nagami: couldn’t install a current agami-core into python3.\n' } : undefined),
  })
  const ui = await pressConnect($)

  await ui.press({ key: 'setup-store' })

  expect(w.spawned).toHaveLength(1)
  expect(w.sent).toHaveLength(1)
  expect(w.sent[0]).toMatch(/Phase 0a\.4/)
  expect(await ui.find({ type: 'Text', text: '/art/local/credentials.example' })).toBeDefined()
  expect(
    await ui.find({ type: 'Text', text: 'Secure storage could not be set up: agami: couldn’t install a current agami-core into python3.' }),
  ).toBeDefined()
  await ui.unmount()
})

test('another refusal shows its detail and keeps the form', async ($, on) => {
  await openConnect($, on, [
    line({ status: 'refused', reason: 'saved_password_exists', profile: 'demo', detail: 'A saved password for demo already exists on this computer.' }),
  ])
  const ui = await pressConnect($)

  expect(await ui.find({ type: 'Text', text: /A saved password for demo already exists/ })).toBeDefined()
  expect(await ui.find({ type: 'Button', key: 'setup-store' })).toBeUndefined()
  expect(await ui.find({ type: 'Input', key: 'profile' })).toBeDefined()
  await ui.unmount()
})

const MISSING = line({
  status: 'missing_driver',
  package: 'psycopg2-binary',
  profile: 'demo',
  detail: 'The postgres driver is not installed for this Python. Install psycopg2-binary, then connect again.',
})

test('a missing driver offers to install that package', async ($, on) => {
  const w = await openConnect($, on, [MISSING])
  const ui = await pressConnect($)

  expect(await ui.find({ type: 'Text', text: /postgres driver is not installed/ })).toBeDefined()
  expect((await ui.find({ type: 'Button', key: 'install-driver' }))?.text).toBe('Install psycopg2-binary')
  expect(w.argvs.filter(a => pipArgs(a) !== undefined)).toEqual([])
  await ui.unmount()
})

test('Install runs pip through the plugin’s Python, then starts the helper again', async ($, on) => {
  const w = await openConnect($, on, [MISSING], { data: READY }, { then: [CONNECTED] })
  const ui = await pressConnect($)

  await ui.press({ key: 'install-driver' })

  expect(w.argvs.map(pipArgs).filter(Boolean)).toEqual([['-m', 'pip', 'install', '--user', 'psycopg2-binary']])
  expect(w.spawned).toHaveLength(2)
  expect(w.sent).toEqual([CONTINUE])
  await ui.unmount()
})

test('a Python that refuses --user installs without it', async ($, on) => {
  const w = await openConnect($, on, [MISSING], {
    data: READY,
    run: argv => (pipArgs(argv)?.includes('--user') ? { exitCode: 1, stderr: 'ERROR: Can not perform a --user install.\n' } : undefined),
  }, { then: [CONNECTED] })
  const ui = await pressConnect($)

  await ui.press({ key: 'install-driver' })

  expect(w.argvs.map(pipArgs).filter(Boolean)).toEqual([
    ['-m', 'pip', 'install', '--user', 'psycopg2-binary'],
    ['-m', 'pip', 'install', 'psycopg2-binary'],
  ])
  expect(w.sent).toEqual([CONTINUE])
  await ui.unmount()
})

test('a failed driver install shows pip’s last line and keeps Try again', async ($, on) => {
  let fails = true
  const w = await openConnect($, on, [MISSING], {
    data: READY,
    run: argv =>
      pipArgs(argv) !== undefined && fails
        ? { exitCode: 1, stderr: 'Collecting psycopg2-binary\nERROR: No matching distribution found for psycopg2-binary\n' }
        : undefined,
  }, { then: [CONNECTED] })
  const ui = await pressConnect($)

  await ui.press({ key: 'install-driver' })

  expect(await ui.find({ type: 'Text', text: 'ERROR: No matching distribution found for psycopg2-binary' })).toBeDefined()
  expect((await ui.find({ type: 'Button', key: 'install-driver' }))?.text).toBe('Try again')
  expect(w.spawned).toHaveLength(1)

  fails = false
  await ui.press({ key: 'install-driver' })
  expect(w.spawned).toHaveLength(2)
  expect(w.sent).toEqual([CONTINUE])
  await ui.unmount()
})

test('several driver packages go to pip as separate arguments', async ($, on) => {
  const w = await openConnect($, on, [line({ status: 'missing_driver', package: 'duckdb pytz', profile: 'demo', detail: 'x' })])
  const ui = await pressConnect($)

  expect((await ui.find({ type: 'Button', key: 'install-driver' }))?.text).toBe('Install duckdb pytz')
  await ui.press({ key: 'install-driver' })
  expect(w.argvs.map(pipArgs).filter(Boolean)[0]).toEqual(['-m', 'pip', 'install', '--user', 'duckdb', 'pytz'])
  await ui.unmount()
})

// --- After connecting, the new profile is the active one. ---

test('a connected profile becomes active: AGAMI_PROFILE is set and home heads with it', async ($, on) => {
  const sets: [string, string | undefined][] = []
  on('env.set', (_, e) => {
    sets.push([e.name, e.value])

    return { value: {} }
  })
  const w = await openConnect($, on, [CONNECTED])
  const ui = await pressConnect($)

  expect(sets).toEqual([['AGAMI_PROFILE', 'demo']])
  expect(w.sent).toEqual([CONTINUE])
  expect(await ui.find({ type: 'Button', text: 'Connect a database' })).toBeDefined()
  expect(await ui.find({ type: 'Text', text: /^demo$/ })).toBeDefined()
  await ui.unmount()
})

test('a held AGAMI_PROFILE still hands off, and home does not claim the profile is active', async ($, on) => {
  on('env.set', () => ({ value: { deny: 'Your organization holds this variable.' } }))
  const w = await openConnect($, on, [CONNECTED])
  const ui = await pressConnect($)

  expect(w.sent).toEqual([CONTINUE])
  expect(await ui.find({ type: 'Button', text: 'Connect a database' })).toBeDefined()
  expect(await ui.find({ type: 'Text', text: /^demo$/ })).toBeUndefined()
  await ui.unmount()
})

// --- Windows: every command runs through Git Bash, never WSL's bash. ---

test('on Windows the helper and its cleanup run through the Git Bash the check found', async ($, on) => {
  const gitBash = 'C:\\Program Files\\Git\\bin\\bash.exe'
  const w = await openConnect($, on, [MISSING], {
    data: { ...READY, platform: 'Windows' },
    env: { OS: 'Windows_NT', CLAUDE_CODE_GIT_BASH_PATH: gitBash },
  }, { then: [CONNECTED] })
  const ui = await pressConnect($)
  await ui.press({ key: 'install-driver' })

  expect(w.spawned.map(a => a[0])).toEqual([gitBash, gitBash])
  expect(w.argvs.filter(a => a[0] === 'bash')).toEqual([])
  expect(w.argvs.some(a => a[0] === gitBash && a.includes(SPEC_PATH))).toBe(true)
  await ui.unmount()
})
