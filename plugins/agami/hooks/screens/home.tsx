import { atom, read, update } from 'claude-code'
import type { CoreEngineInterface, RenderElement, RenderInput } from 'claude-code'

import type { AgamiEnv, AgamiSampleState, AgamiScreen } from '../../types'
import { BRAND, SOFT, logoSvg } from '../brand'

// Declared in each file that reads or writes them: the engine lists the values a module touches by
// reading the atom where it is used, so an imported atom would not be seen.
const screenState = atom({ plugin: 'agami-core', key: 'screen' } as const, 'home' as AgamiScreen)
const envState = atom({ plugin: 'agami-core', key: 'env' } as const, null as AgamiEnv | null, {
  shape: 'env-3',
})

const IDLE: AgamiSampleState = { waiting: false, turnId: null, note: null }
const sampleState = atom({ plugin: 'agami-core', key: 'sample' } as const, IDLE, { shape: 'sample-2' })

type Engine = Pick<CoreEngineInterface, 'ui' | 'state' | 'prompt' | 'env'>

type Group = 'answers' | 'improve' | 'setup'
type Action = { key: string; label: string; group: Group; prompt?: string }

// Every top-level thing agami does, in plain words, in the order the home screen shows them. Each but
// `connect` starts the matching skill's existing flow; `connect` opens the guided Connect screen.
export const actions: readonly Action[] = [
  {
    key: 'ask',
    label: 'Ask a question',
    group: 'answers',
    prompt: 'I want to ask a question about my data with agami. Ask me what I want to know.',
  },
  {
    key: 'reconcile',
    label: 'Check numbers against a dashboard',
    group: 'answers',
    prompt: 'Run /agami-core:agami-reconcile. Ask me for the dashboard first.',
  },
  { key: 'review', label: 'Review the model', group: 'improve', prompt: 'Run /agami-core:agami-model.' },
  { key: 'golden', label: 'Save and run test questions', group: 'improve', prompt: 'Run /agami-core:agami-eval.' },
  {
    key: 'reload',
    label: 'Reload a changed schema',
    group: 'improve',
    prompt: 'Run /agami-core:agami-connect reintrospect for the active profile.',
  },
  { key: 'connect', label: 'Connect a database', group: 'setup' },
  {
    key: 'sample',
    label: 'Try the sample data',
    group: 'setup',
    prompt: 'Run /agami-core:agami-connect sample and take the "Just let me query it" path.',
  },
  { key: 'serve', label: 'Use agami from other apps', group: 'setup', prompt: 'Run /agami-core:agami-serve.' },
]

const GROUPS: readonly { key: Group; title: string }[] = [
  { key: 'answers', title: 'Get answers' },
  { key: 'improve', title: 'Improve the model' },
  { key: 'setup', title: 'Set up' },
]

// The rest need a model to work from, so they wait until a database is connected.
const FIRST_RUN = new Set(['connect', 'sample'])

/** Runs one action: the Connect screen for `connect`, otherwise the skill's prompt, as the user's own. */
export async function runAction($: Pick<Engine, 'prompt' | 'state'>, key: string): Promise<void> {
  const action = actions.find(a => a.key === key)
  if (!action) return
  await update($, sampleState, () => IDLE)
  if (action.prompt === undefined) {
    await update($, screenState, () => 'connect')

    return
  }
  // Marked before the send so a turn that ends at once is still seen; undone if the prompt never starts one.
  if (key === 'sample') await update($, sampleState, () => ({ waiting: true, turnId: null, note: null }))
  const sent = await $.prompt.submit({ text: action.prompt })
  if (key === 'sample' && sent.drop !== undefined) await update($, sampleState, () => IDLE)
}

/** Puts the sample path back to idle: a fresh open of the pane waits for nothing and shows no note. */
export async function resetSample($: Pick<Engine, 'state'>): Promise<void> {
  await update($, sampleState, () => IDLE)
}

const SAMPLE_PROMPT = actions.find(a => a.key === 'sample')?.prompt ?? ''

/**
 * Called when a main-loop turn starts. While the sample waits, the turn whose text holds the sample's
 * prompt is the sample's: its id is kept so only its own end is acted on. `includes`, because the
 * text may arrive framed.
 */
export async function onTurnStart($: Pick<Engine, 'state'>, turnId: string, text: string): Promise<void> {
  const sample = await read($, sampleState)
  if (!sample.waiting || sample.turnId !== null || !text.includes(SAMPLE_PROMPT)) return
  await update($, sampleState, st => ({ ...st, turnId }))
}

const SAMPLE_NOTE = "The sample setup didn't finish. Check the chat to see where it stopped."

/**
 * Called when any turn ends. If it is the sample's turn, the pane moves to First answer once the
 * sample model's `datasource.yaml` exists; otherwise it stays home and says setup did not finish.
 */
export async function onTurnComplete($: Pick<Engine, 'state'> & Pick<CoreEngineInterface, 'fs'>, turnId: string): Promise<void> {
  const sample = await read($, sampleState)
  if (!sample.waiting || sample.turnId !== turnId) return
  const env = await read($, envState)
  const ready = env?.artifactsDir != null && (await $.fs.exists(`${env.artifactsDir}/agami-example/datasource.yaml`))
  await update($, sampleState, () => ({ waiting: false, turnId: null, note: ready ? null : SAMPLE_NOTE }))
  if (ready) await update($, screenState, () => 'first-answer')
}

export async function home($: Engine, e: RenderInput): Promise<RenderElement> {
  const ui = $.ui.resolve(e)
  const { Box, Text, Button } = ui
  const env = await read($, envState)
  const { note } = await read($, sampleState)

  if (!env) {
    return (
      <Box flexDirection="column">
        <Text bold color={BRAND}>
          agami
        </Text>
        <Text dimColor>Checking your setup…</Text>
      </Box>
    )
  }

  const hasProfile = env.profiles.length > 0
  // A still mark: the terminal has no `Svg`, so there the name alone stands for the logo.
  let logo: RenderElement | null = null
  if (e.surface === 'desktop' || e.surface === 'vscode') {
    const { Svg } = $.ui.resolve(e)
    logo = <Svg source={logoSvg()} alt="agami logo" width={32} />
  }

  // A "profile" is agami's name for one saved database connection; on screen it is just the database.
  let database: RenderElement | null = null
  if (env.active !== null) {
    let switcher: RenderElement | null = null
    if (e.surface !== 'mobile' && env.profiles.length > 1) {
      const { Select } = $.ui.resolve(e)
      switcher = (
        <Select
          key="profile"
          label="Switch database"
          options={env.profiles.map(value => ({ value }))}
          value={env.active}
          onSelect={async (value: string) => {
            // AGAMI_PROFILE is what every agami script reads first, so the pick holds for the session.
            await $.env.set('AGAMI_PROFILE', value)
            await update($, envState, current => (current ? { ...current, active: value } : current))
          }}
        />
      )
    }
    database = (
      <Box flexDirection="column" rowGap="xs">
        <Text dimColor size="caption">
          Connected database
        </Text>
        <Text size="heading" weight="semibold">
          {env.active}
        </Text>
        {switcher}
      </Box>
    )
  }

  const usable = actions.filter(a => env.python.ok || !FIRST_RUN.has(a.key))
  const button = (a: Action, primary: boolean): RenderElement => (
    <Button key={a.key} label={a.label} variant={primary ? 'primary' : 'secondary'} onPress={() => runAction($, a.key)} />
  )

  const body = !hasProfile ? (
    <Box flexDirection="column" rowGap="md" paddingY="sm">
      <Text>Connect your database to start asking questions. Credentials and data stay on this computer.</Text>
      <Box flexDirection="column" alignItems="flex-start" rowGap="sm">
        {usable.filter(a => FIRST_RUN.has(a.key)).map(a => button(a, a.key === 'connect'))}
      </Box>
    </Box>
  ) : (
    <Box flexDirection="column" rowGap="lg">
      {GROUPS.map(g => {
        const items = usable.filter(a => a.group === g.key)
        if (items.length === 0) return null

        return (
          <Box key={g.key} flexDirection="column" rowGap="sm">
            <Text dimColor size="caption" weight="medium">
              {g.title}
            </Text>
            <Box flexDirection="column" alignItems="flex-start" rowGap="xs">
              {items.map(a => button(a, a.key === 'ask'))}
            </Box>
          </Box>
        )
      })}
    </Box>
  )

  return (
    <Box flexDirection="column" rowGap="lg" padding="md">
      <Box flexDirection="row" alignItems="center" columnGap="md">
        {logo}
        <Text size="heading" weight="semibold" color={BRAND}>
          agami
        </Text>
      </Box>
      {database !== null ? (
        <Box borderStyle="round" borderColor={SOFT} paddingX="md" paddingY="sm">
          {database}
        </Box>
      ) : null}
      {!env.python.ok ? <Text>{env.python.fix ?? 'agami cannot find a usable Python.'}</Text> : null}
      {note !== null ? <Text>{note}</Text> : null}
      {body}
    </Box>
  )
}
