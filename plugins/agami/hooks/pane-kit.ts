// Shared by the tests: opens the pane the way a person does, with the engine beneath the plugin
// stood in for, so the screens draw from the environment the test names.
import type { TestBody } from 'claude-code/testing'

type Hooks = Parameters<TestBody>[1]
export type Engine = Parameters<TestBody>[0]

export const PANE = {
  plugin: 'agami-core',
  component: 'Pane',
  requestId: 'agami',
  props: {
    title: 'agami',
    isFocused: false,
    bodyColumns: 48,
    placement: 'dock',
    scroll: { offset: 0, bodyRows: 24 },
    view: {},
  },
} as const

export const SURFACES = ['terminal', 'desktop', 'vscode'] as const

export type Resolve = {
  data?: Record<string, unknown>
  anomalies?: object[]
  exitCode?: number
  stderr?: string
  /** What `uname` answers when the script could not run; absent, `uname` is missing (Windows). */
  uname?: string
  /** What another plugin script prints, by its name (`promote_credentials.py`); a function reads it afresh per run. */
  scripts?: Record<string, string | (() => string)>
}

const QUIET = { stderr: '', isStdoutTruncated: false, isStderrTruncated: false } as const

export const READY = {
  profile: null,
  profiles: [],
  platform: 'Darwin',
  artifacts_dir: '/art',
  interpreter: { chosen_because: 'ok' },
}

/** Runs `/agami` with `connect_resolve.py` answering as named, so the pane has read its environment. */
export async function openPane($: Engine, on: Hooks, resolve: Resolve = { data: READY }): Promise<string[][]> {
  const argvs: string[][] = []
  on('command.register', (_, e) => ({ value: { command: e.name } }))
  on('ui.open', () => ({ value: { isPlaced: true } }))
  on('session.start', (_, e) => ({ cwd: e.cwd }))
  on('process.run', (_, e) => {
    argvs.push([...e.argv])
    if (e.argv[0] === 'uname') {
      if (resolve.uname === undefined) throw new Error('uname: not found')

      return { value: { ...QUIET, exitCode: 0, stdout: `${resolve.uname}\n` } }
    }
    if (e.argv[0] !== 'bash' || e.argv[2] !== 'connect_resolve.py') {
      const answer = resolve.scripts?.[e.argv[2] ?? '']
      const stdout = typeof answer === 'function' ? answer() : (answer ?? '')

      return { value: { ...QUIET, exitCode: 0, stdout } }
    }

    return {
      value: {
        ...QUIET,
        exitCode: resolve.exitCode ?? 0,
        stderr: resolve.stderr ?? '',
        stdout: JSON.stringify({ ok: true, data: resolve.data ?? {}, anomalies: resolve.anomalies ?? [], needs_judgment: null }),
      },
    }
  })

  await $.session.start({ cwd: '/', surface: 'desktop', isInteractive: true })
  await $.command.run({
    command: 'agami',
    args: '',
    origin: { kind: 'composer' },
    presentation: { isFullscreen: true, columns: 160 },
  })

  return argvs
}
