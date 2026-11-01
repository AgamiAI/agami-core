import { atom, read, update } from 'claude-code'
import type { CoreEngineInterface, HookStream, ProcessSpawnChunk, ProcessSpawnResult, RenderElement, RenderInput } from 'claude-code'

import type { AgamiConnectState, AgamiEnv, AgamiScreen } from '../../types'
import { BRAND } from '../brand'
import { readEnv } from '../env'

// Declared in each file that reads or writes them: the engine lists the values a module touches by
// reading the atom where it is used, so an imported atom would not be seen.
const screenState = atom({ plugin: 'agami-core', key: 'screen' } as const, 'home' as AgamiScreen)
const envState = atom({ plugin: 'agami-core', key: 'env' } as const, null as AgamiEnv | null, {
  shape: 'env-2',
})
const FORM: AgamiConnectState = { phase: 'form', dbType: 'postgres', fields: {} }
const connectState = atom({ plugin: 'agami-core', key: 'connect' } as const, FORM, { shape: 'connect-1' })

type Engine = Pick<CoreEngineInterface, 'ui' | 'state' | 'prompt' | 'process' | 'fs' | 'plugin'>

/** Merges `change` into the Connect screen's state, read afresh so two quick writes both land. */
function patch($: Pick<Engine, 'state'>, change: Partial<AgamiConnectState>): Promise<unknown> {
  return update($, connectState, (st): AgamiConnectState => ({ ...st, ...change }))
}

type Field = { key: string; label: string; placeholder?: string; fallback?: string }

const SERVER = (port: string): Field[] => [
  { key: 'host', label: 'Host', placeholder: 'your-host.example.com' },
  { key: 'port', label: 'Port', placeholder: port, fallback: port },
  { key: 'database', label: 'Database' },
  { key: 'user', label: 'User' },
]

// The non-secret fields per type, keyed as the credentials file spells them. No type lists a
// password: it is typed into the helper's page, never into the pane.
const DB_TYPES: readonly { value: string; label: string; fields: Field[] }[] = [
  { value: 'postgres', label: 'Postgres', fields: SERVER('5432') },
  { value: 'mysql', label: 'MySQL', fields: SERVER('3306') },
  {
    value: 'snowflake',
    label: 'Snowflake',
    fields: [
      { key: 'account', label: 'Account', placeholder: 'xy12345.us-east-1' },
      { key: 'user', label: 'User' },
      { key: 'warehouse', label: 'Warehouse' },
      { key: 'database', label: 'Database' },
    ],
  },
  { value: 'sqlserver', label: 'SQL Server', fields: SERVER('1433') },
  { value: 'other', label: 'Other', fields: [] },
]

const PROFILE_RE = /^[a-z0-9_-]{1,32}$/
const LINK_NOTE = 'Opens a page on this computer. The password goes to your keychain, not the chat.'
const EXPIRED = 'The page expired before a password was entered. Connect again when you are ready.'

export const continuePrompt = (profile: string): string =>
  `The agami profile ${profile} is connected. Continue /agami-core:agami-connect from Phase 1.3 for it (skip Phase 0).`

const fieldsOf = (dbType: string | undefined): Field[] => DB_TYPES.find(d => d.value === dbType)?.fields ?? []

// The helper the person is waiting on, if any. A process handle cannot live in `$.state`; a reload
// of this module kills the child anyway, so nothing outlives the variable.
let running: { stop: () => void } | null = null

/** Stops a waiting helper and puts the Connect screen back to an empty form. */
export async function resetConnect($: Pick<Engine, 'state'>): Promise<void> {
  const helper = running
  running = null
  helper?.stop()
  await update($, connectState, () => FORM)
}

function profileProblem(profile: string, env: AgamiEnv): string | null {
  if (!PROFILE_RE.test(profile)) {
    return `The profile name "${profile}" is not valid. Use 1 to 32 lowercase letters, digits, hyphens or underscores, for example demo.`
  }
  if (env.profiles.includes(profile)) return `There is already a profile named ${profile}. Pick another name.`

  return null
}

function specOf(st: AgamiConnectState): Record<string, unknown> {
  const sso = st.dbType === 'snowflake' && st.auth === 'sso'
  const fields: Record<string, string> = {}
  for (const f of fieldsOf(st.dbType)) {
    const value = (st.fields?.[f.key] ?? '').trim() || f.fallback || ''
    if (value !== '') fields[f.key] = value
  }
  if (sso) fields.authenticator = 'externalbrowser'

  return { profile: st.profile, db_type: st.dbType, ...(sso ? { auth: 'sso' } : {}), fields }
}

const specPath = (env: AgamiEnv, profile: string): string => `${env.artifactsDir}/local/tmp/connect-${profile}.json`

// No `$.fs` call removes a file; `bash` is already what every agami script runs through.
async function removeSpec($: Pick<Engine, 'process'>, path: string): Promise<void> {
  try {
    await $.process.run(['bash', '-c', 'rm -f -- "$1"', 'agami', path])
  } catch {
    // The spec holds no secret; a leftover file is untidy, not a leak.
  }
}

/** The end every successful path shares: Claude takes over, the pane goes home and reads the new profile. */
async function finishConnected($: Engine, profile: string): Promise<void> {
  await $.prompt.submit({ text: continuePrompt(profile) })
  await update($, connectState, () => FORM)
  await update($, screenState, () => 'home')
  const env = await readEnv($)
  await update($, envState, () => env)
}

type Final = { status?: string; detail?: string; url?: string }

async function onLine($: Engine, msg: Final, profile: string): Promise<boolean> {
  if (typeof msg.url === 'string') {
    await patch($, { phase: 'waiting', url: msg.url })

    return false
  }
  if (msg.status === 'connected') {
    await finishConnected($, profile)
  } else if (msg.status === 'expired') {
    await patch($, { phase: 'form', url: undefined, detail: EXPIRED })
  } else {
    await patch($, { phase: 'failed', url: undefined, detail: msg.detail || 'The connection could not be made.' })
  }

  return true
}

/**
 * Reads the helper's JSON lines as they arrive. It runs detached from the press that started it: the
 * helper waits up to 300 s for the password, far past a hook's 10 s budget.
 */
async function watchHelper($: Engine, path: string, profile: string): Promise<void> {
  const stream: HookStream<ProcessSpawnChunk, ProcessSpawnResult> = $.process.spawn({
    argv: ['bash', `${$.plugin.root}/scripts/py`, 'connect_helper.py', '--spec', path],
  })
  // Ending the stream kills the child. Not awaited, so a close or a Back never waits on the helper.
  const mine = {
    stop: () => {
      stream.return(undefined as unknown as ProcessSpawnResult).catch(() => undefined)
    },
  }
  running = mine
  let ended = false
  let pending = ''
  let heard = false
  try {
    for await (const chunk of stream) {
      heard = true
      if (running !== mine) break
      if (chunk.stream !== 'stdout') continue
      pending += chunk.text
      let cut = pending.indexOf('\n')
      while (cut >= 0 && !ended) {
        const text = pending.slice(0, cut).trim()
        pending = pending.slice(cut + 1)
        cut = pending.indexOf('\n')
        let msg: Final
        try {
          msg = JSON.parse(text) as Final
        } catch {
          // Not a line of the helper's contract (a stray print); the final line still comes.
          continue
        }
        ended = await onLine($, msg, profile)
      }
      if (ended) break
    }
  } catch {
    if (running === mine) {
      const detail = heard ? 'The connect helper stopped unexpectedly.' : 'The connect helper could not start.'
      await patch($, { phase: 'failed', url: undefined, detail })
      ended = true
    }
  } finally {
    await removeSpec($, path)
  }
  if (running !== mine) return
  running = null
  if (!ended) {
    await patch($, { phase: 'failed', url: undefined, detail: 'The connect helper stopped without saying how it ended.' })
  }
}

const manualPrompt = (dbType: string, profile: string): string =>
  `Run /agami-core:agami-connect Phase 0a.4 only: write the credentials.example template for a ${dbType} ` +
  `database with the profile name ${profile}, then stop. I will fill it in myself.`

const otherPrompt = (profile: string): string =>
  `Run /agami-core:agami-connect to set up a new database profile named ${profile}. My database is not ` +
  'Postgres, MySQL, Snowflake or SQL Server; ask me which one it is.'

async function start($: Engine): Promise<void> {
  // A second press before the pane redrew would start a second helper beside the first.
  if (running !== null) return
  const env = await read($, envState)
  const st = await read($, connectState)
  const profile = (st.profile ?? '').trim()
  if (!env?.artifactsDir) {
    await patch($, { detail: 'agami could not find its folder. Close the pane and open it again.' })

    return
  }
  const problem = profileProblem(profile, env)
  if (problem) {
    await patch($, { detail: problem })

    return
  }
  const dbType = st.dbType ?? 'postgres'
  if (dbType === 'other') {
    await $.prompt.submit({ text: otherPrompt(profile) })
    await update($, connectState, () => FORM)
    await update($, screenState, () => 'home')

    return
  }
  if (env.os === 'other') {
    await $.prompt.submit({ text: manualPrompt(dbType, profile) })
    await patch($, { profile, phase: 'manual', detail: undefined })

    return
  }
  const path = specPath(env, profile)
  try {
    await $.fs.write(path, JSON.stringify(specOf({ ...st, profile, dbType })))
  } catch {
    await patch($, { detail: `agami could not write ${path}. Check that its folder can be written to.` })

    return
  }
  await patch($, { profile, phase: 'waiting', url: undefined, detail: undefined })
  void watchHelper($, path, profile).catch(() => {
    // The pane is gone (closed, or the module reloaded) and its state with it; nothing to tell.
  })
}

const PROMOTE_MESSAGES: Record<string, string> = {
  PLACEHOLDERS_REMAIN: 'The template still has placeholder values. Fill them in, save the file, then check again.',
  COLLISION: 'A profile with this name is already in your credentials file. Go back and pick another name.',
  NOTHING: 'There is no credentials.example yet. Wait for Claude to write it, then check again.',
}

async function checkAgain($: Engine): Promise<void> {
  const st = await read($, connectState)
  let first = ''
  let rest = ''
  try {
    const run = await $.process.run(['bash', `${$.plugin.root}/scripts/py`, 'promote_credentials.py'])
    const out = run.stdout.trim()
    first = out.split(/\s+/)[0] ?? ''
    rest = out.slice(first.length).trim()
  } catch {
    first = 'ERROR'
  }
  if (first === 'SECURED' || first === 'APPENDED') {
    await finishConnected($, st.profile ?? '')

    return
  }
  const detail = PROMOTE_MESSAGES[first] ?? `The template could not be used${rest ? `: ${rest}` : '.'}`
  await patch($, { detail })
}

async function back($: Engine): Promise<void> {
  await resetConnect($)
  await update($, screenState, () => 'home')
}

export async function connect($: Engine, e: RenderInput): Promise<RenderElement> {
  const ui = $.ui.resolve(e)
  const { Box, Text, Button } = ui
  const st = await read($, connectState)
  const env = await read($, envState)
  const header = (
    <Text bold color={BRAND}>
      Connect a database
    </Text>
  )
  const backButton = <Button key="back" label="Back" onPress={() => back($)} />
  const detail = st.detail ? <Text>{st.detail}</Text> : null

  if (st.phase === 'waiting') {
    let body: RenderElement
    if (st.auth === 'sso' && st.dbType === 'snowflake') {
      body = <Text>Signing in through your browser. Finish there and this updates.</Text>
    } else if (st.url) {
      const { Link } = ui
      const url = st.url
      // Desktop and VS Code make a link only of https: and http://localhost, and the helper answers
      // on 127.0.0.1 alone (its Host check), so there the address is also shown to copy.
      const copy =
        e.surface === 'terminal' ? null : (
          <Box flexDirection="column">
            <Text>{url}</Text>
            <Button key="copy" label="Copy the link" onPress={press => $.ui.copy({ text: url, surface: press.surface })} />
          </Box>
        )
      body = (
        <Box flexDirection="column">
          <Link href={url} label="Enter the password" />
          {copy}
          <Text>{LINK_NOTE}</Text>
          <Text dimColor>The page closes after 5 minutes.</Text>
        </Box>
      )
    } else {
      body = <Text dimColor>Starting…</Text>
    }

    return (
      <Box flexDirection="column">
        {header}
        <Text>Profile {st.profile}</Text>
        <Box flexDirection="column" marginTop={1}>
          {body}
        </Box>
        <Box flexDirection="column" marginTop={1}>
          {backButton}
        </Box>
      </Box>
    )
  }

  if (st.phase === 'failed') {
    return (
      <Box flexDirection="column">
        {header}
        <Text>The connection to {st.profile} did not work.</Text>
        {detail}
        <Box flexDirection="column" marginTop={1}>
          <Button
            key="retry"
            label="Try again"
            variant="primary"
            onPress={() => patch($, { phase: 'form', detail: undefined })}
          />
          {backButton}
        </Box>
      </Box>
    )
  }

  if (st.phase === 'manual') {
    return (
      <Box flexDirection="column">
        {header}
        <Text>This system has no password store agami can use, so the password goes in a file you edit.</Text>
        <Text>Claude is writing the template. Fill in the values, save it, then check again.</Text>
        <Text bold>{`${env?.artifactsDir ?? '<artifacts_dir>'}/local/credentials.example`}</Text>
        {detail}
        <Box flexDirection="column" marginTop={1}>
          <Button key="check" label="Check again" variant="primary" onPress={() => checkAgain($)} />
          {backButton}
        </Box>
      </Box>
    )
  }

  // The phone has no `Input` or `Select`, so the form cannot be drawn there.
  if (e.surface === 'mobile') {
    return (
      <Box flexDirection="column">
        {header}
        <Text>Connect a database from the terminal or the desktop app.</Text>
        {backButton}
      </Box>
    )
  }

  const { Input, Select } = $.ui.resolve(e)
  const setField = (key: string) => (value: string) =>
    update($, connectState, (s): AgamiConnectState => ({ ...s, fields: { ...s.fields, [key]: value } }))
  const setProfile = (value: string) => patch($, { profile: value.trim(), detail: undefined })

  return (
    <Box flexDirection="column">
      {header}
      <Select
        key="dbType"
        label="Database type"
        options={DB_TYPES.map(({ value, label }) => ({ value, label }))}
        value={st.dbType ?? 'postgres'}
        onSelect={(value: string) => patch($, { dbType: value, detail: undefined })}
      />
      {st.dbType === 'snowflake' ? (
        <Select
          key="auth"
          label="Sign-in"
          options={[
            { value: 'password', label: 'Typed in a page on this computer' },
            { value: 'sso', label: 'Browser sign-in' },
          ]}
          value={st.auth ?? 'password'}
          onSelect={(value: string) => patch($, { auth: value === 'sso' ? 'sso' : 'password' })}
        />
      ) : null}
      <Input
        key="profile"
        label="Profile name"
        placeholder="for example demo"
        value={st.profile ?? ''}
        onInput={setProfile}
        onSubmit={setProfile}
      />
      {fieldsOf(st.dbType).map(f => (
        <Input
          key={f.key}
          label={f.label}
          placeholder={f.placeholder}
          value={st.fields?.[f.key] ?? ''}
          onInput={setField(f.key)}
          onSubmit={setField(f.key)}
        />
      ))}
      {detail}
      <Box flexDirection="column" marginTop={1}>
        <Button key="start" label="Connect" variant="primary" onPress={() => start($)} />
        {backButton}
      </Box>
    </Box>
  )
}
