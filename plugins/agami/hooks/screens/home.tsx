import { atom, read, update } from 'claude-code'
import type { CoreEngineInterface, RenderElement, RenderInput } from 'claude-code'

import type { AgamiEnv, AgamiScreen } from '../../types'
import { ACCENT, BRAND, logoSvg, logoSvgAnimated } from '../brand'

// Declared in each file that reads or writes them: the engine lists the values a module touches by
// reading the atom where it is used, so an imported atom would not be seen.
const screenState = atom({ plugin: 'agami-core', key: 'screen' } as const, 'home' as AgamiScreen)
const envState = atom({ plugin: 'agami-core', key: 'env' } as const, null as AgamiEnv | null, {
  shape: 'env-1',
})

type Engine = Pick<CoreEngineInterface, 'ui' | 'state' | 'prompt' | 'env'>

type Action = { key: string; label: string; prompt?: string }

// Every top-level thing agami does, in plain words. Each but `connect` starts the matching skill's
// existing flow; `connect` opens the guided Connect screen instead of asking the model anything.
export const actions: readonly Action[] = [
  { key: 'connect', label: 'Connect a database' },
  {
    key: 'sample',
    label: 'Try the sample data',
    prompt: 'Run /agami-core:agami-connect sample and take the "Just let me query it" path.',
  },
  {
    key: 'ask',
    label: 'Ask a question',
    prompt: 'I want to ask a question about my data with agami. Ask me what I want to know.',
  },
  { key: 'review', label: 'Review the model', prompt: 'Run /agami-core:agami-model.' },
  {
    key: 'reconcile',
    label: 'Check numbers against a dashboard',
    prompt: 'Run /agami-core:agami-reconcile. Ask me for the dashboard first.',
  },
  { key: 'golden', label: 'Save and run test questions', prompt: 'Run /agami-core:agami-eval.' },
  {
    key: 'reload',
    label: 'Reload a changed schema',
    prompt: 'Run /agami-core:agami-connect reintrospect for the active profile.',
  },
  { key: 'serve', label: 'Use agami from other apps', prompt: 'Run /agami-core:agami-serve.' },
]

// The rest need a model to work from, so they wait until a database is connected.
const FIRST_RUN = new Set(['connect', 'sample'])

/** Runs one action: the Connect screen for `connect`, otherwise the skill's prompt, as the user's own. */
export async function runAction($: Pick<Engine, 'prompt' | 'state'>, key: string): Promise<void> {
  const action = actions.find(a => a.key === key)
  if (!action) return
  if (action.prompt === undefined) {
    await update($, screenState, () => 'connect')

    return
  }
  await $.prompt.submit({ text: action.prompt })
}

export async function home($: Engine, e: RenderInput): Promise<RenderElement> {
  const ui = $.ui.resolve(e)
  const { Box, Text, Button } = ui
  const env = await read($, envState)

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
  const shown = hasProfile ? actions : actions.filter(a => FIRST_RUN.has(a.key))
  const leads = !hasProfile
  // The terminal has no `Svg`: its logo is the name, drawn below as text on every surface.
  let logo: RenderElement | null = null
  if (e.surface === 'desktop') {
    const { Svg } = $.ui.resolve(e)
    logo = <Svg source={logoSvgAnimated()} alt="agami logo" isInteractive width={48} />
  } else if (e.surface === 'vscode') {
    const { Svg } = $.ui.resolve(e)
    logo = <Svg source={logoSvg()} alt="agami logo" width={48} />
  }

  // The phone has no `Select`; the profile is still named in the header.
  let profile: RenderElement | null = null
  if (e.surface !== 'mobile' && env.active !== null) {
    const { Select } = $.ui.resolve(e)
    profile = (
      <Select
        key="profile"
        label="Profile"
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

  return (
    <Box flexDirection="column">
      <Box flexDirection="row">
        {logo}
        <Box flexDirection="column">
          <Text bold color={BRAND}>
            agami
          </Text>
          {env.active !== null && (
            <Text bold color={ACCENT}>
              {env.active}
            </Text>
          )}
        </Box>
      </Box>
      {profile}
      {!env.python.ok ? (
        <Box flexDirection="column" marginTop={1}>
          <Text>{env.python.fix ?? 'agami cannot find a usable Python.'}</Text>
        </Box>
      ) : null}
      <Box flexDirection="column" marginTop={1}>
        {shown
          .filter(a => env.python.ok || !FIRST_RUN.has(a.key))
          .map(a => (
            <Button
              key={a.key}
              label={a.label}
              variant={leads && a.key === 'connect' ? 'primary' : undefined}
              onPress={() => runAction($, a.key)}
            />
          ))}
      </Box>
    </Box>
  )
}
