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
  /** `connect_resolve.py` cannot start at all: running it rejects with this message. */
  spawnError?: string
  /** What another plugin script prints, by its name (`promote_credentials.py`); a function reads it afresh per run. */
  scripts?: Record<string, string | (() => string)>
  /** Answers any other command first, by its argv; `undefined` falls through to `scripts`. */
  run?: (argv: string[]) => { exitCode?: number; stdout?: string; stderr?: string } | undefined
  /** The variables the pane reads (`OS`, `CLAUDE_CODE_GIT_BASH_PATH`); unnamed ones read as unset. */
  env?: Record<string, string>
  /** What `where git` prints (Windows); absent, it finds nothing. */
  whereGit?: string
  /** The paths `$.fs.exists` finds; absent, the test answers `fs.exists` itself (or nothing does). */
  files?: string[]
}

// The variables the pane itself reads, answered from the test so the machine running it never decides.
const PANE_ENV = new Set(['OS', 'CLAUDE_CODE_GIT_BASH_PATH'])

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
  on('env.get', (_, e, next) => (PANE_ENV.has(e.name) ? { value: resolve.env?.[e.name] } : next(e)))
  if (resolve.files !== undefined) {
    const files = resolve.files
    // A Windows path is relative on the POSIX host running the tests, so the engine hands it on resolved.
    on('fs.exists', (_, e) => ({ value: files.some(f => e.path === f || e.path.endsWith(`/${f}`)) }))
  }
  on('process.run', (_, e) => {
    argvs.push([...e.argv])
    const custom = resolve.run?.([...e.argv])
    if (custom !== undefined) {
      return { value: { ...QUIET, exitCode: custom.exitCode ?? 0, stdout: custom.stdout ?? '', stderr: custom.stderr ?? '' } }
    }
    if (e.argv[0] === 'where') {
      const found = resolve.whereGit

      return { value: { ...QUIET, exitCode: found ? 0 : 1, stdout: found ?? '' } }
    }
    if (e.argv[2] !== 'connect_resolve.py') {
      const answer = resolve.scripts?.[e.argv[2] ?? '']
      const stdout = typeof answer === 'function' ? answer() : (answer ?? '')

      return { value: { ...QUIET, exitCode: 0, stdout } }
    }

    // A deny rejects the caller, as a command that cannot start does.
    if (resolve.spawnError !== undefined) return { deny: resolve.spawnError }

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
