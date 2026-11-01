import type { CoreEngineInterface } from 'claude-code'

import type { AgamiEnv, AgamiOs } from '../types'

type Engine = Pick<CoreEngineInterface, 'plugin' | 'process' | 'state' | 'env' | 'fs'>

type Resolved = {
  data?: {
    profile?: string | null
    profiles?: string[]
    platform?: string
    artifacts_dir?: string
    interpreter?: { agami_core?: string | null; chosen_because?: string }
  }
  anomalies?: { kind?: string; detail?: string }[]
}

const INSTALL_PYTHON = 'agami needs Python 3.10 or newer. Install it from python.org, then open the pane again.'
const NO_GIT_BASH = 'Git Bash was not found. Install Git for Windows, or set CLAUDE_CODE_GIT_BASH_PATH to its bash.exe.'
const cannotRun = (reason: string): string => `Couldn't run agami's setup check: ${reason}`

function osOf(platform: string | undefined): AgamiOs {
  return platform === 'Windows' ? 'windows' : platform === 'Darwin' ? 'mac' : 'other'
}

const lastLine = (text: string): string =>
  text
    .split(/\r?\n/)
    .map(l => l.trim())
    .filter(Boolean)
    .pop() ?? ''

// Windows sets OS=Windows_NT for every process; nothing else does. A variable that cannot be read is not Windows.
async function onWindows($: Pick<Engine, 'env'>): Promise<boolean> {
  try {
    return (await $.env.get('OS')) === 'Windows_NT'
  } catch {
    return false
  }
}

// git.exe sits in <Git>\cmd, <Git>\bin or <Git>\mingw64\bin; Git Bash is <Git>\bin\bash.exe.
const GIT_EXE = /\\(?:mingw64\\bin|cmd|bin)\\git\.exe$/i

/**
 * The bash every agami command runs through. On Windows a bare `bash` can be WSL's (System32\bash.exe),
 * which cannot see the Windows Python or paths, so it is Git Bash: CLAUDE_CODE_GIT_BASH_PATH (the
 * setting Claude Code itself reads), else the bash.exe that ships beside the git.exe on the PATH.
 */
export async function resolveBash($: Pick<Engine, 'env' | 'process' | 'fs'>): Promise<{ path: string } | { error: string }> {
  if (!(await onWindows($))) return { path: 'bash' }
  let pinned: string | undefined
  try {
    pinned = (await $.env.get('CLAUDE_CODE_GIT_BASH_PATH'))?.trim()
  } catch {
    pinned = undefined
  }
  if (pinned) return { path: pinned }
  let found = ''
  try {
    found = (await $.process.run(['where', 'git'])).stdout
  } catch {
    found = ''
  }
  for (const git of found.split(/\r?\n/).map(l => l.trim())) {
    if (!GIT_EXE.test(git)) continue
    const bash = `${git.replace(GIT_EXE, '')}\\bin\\bash.exe`
    if (/\\system32\\/i.test(bash)) continue
    try {
      if (await $.fs.exists(bash)) return { path: bash }
    } catch {
      // A path the store cannot look at is not this one; try the next git.
    }
  }

  return { error: NO_GIT_BASH }
}

// Python is unusable only when none was found, or every one found was skipped because it can never
// be brought current (no pip and an older agami-core). Missing model dependencies are not that:
// agami-connect installs them on the first connect.
function pythonOf(out: Resolved): AgamiEnv['python'] {
  const stuck = out.anomalies?.find(a => a.kind === 'agami_python_cannot_be_brought_current')
  const interpreter = out.data?.interpreter
  const none = interpreter?.chosen_because?.startsWith('no candidate interpreter found')
  const skipped = interpreter?.chosen_because?.startsWith('every candidate was skipped')
  if (none) return { ok: false, fix: INSTALL_PYTHON }
  if (skipped || stuck) {
    return { ok: false, fix: stuck?.detail ?? 'The Python agami found cannot install packages. Install Python from python.org.' }
  }

  return { ok: true }
}

/** Runs `connect_resolve.py` once and reads what the home screen needs. Never throws and never reads a password. */
export async function readEnv($: Engine): Promise<AgamiEnv> {
  // When the check cannot say which system this is, only Windows is known for sure (it is how bash was chosen).
  const isWindows = await onWindows($)
  const failed = (fix: string, bash: string | null): AgamiEnv => ({
    profiles: [],
    active: null,
    python: { ok: false, fix },
    os: isWindows ? 'windows' : 'other',
    artifactsDir: null,
    bash,
  })
  const bash = await resolveBash($)
  if ('error' in bash) return failed(cannotRun(bash.error), null)
  const script = `${$.plugin.root}/scripts/py`
  let run: Awaited<ReturnType<Engine['process']['run']>>
  try {
    run = await $.process.run([bash.path, script, 'connect_resolve.py'], { timeoutMs: 60_000 })
  } catch (err) {
    return failed(cannotRun(err instanceof Error ? err.message : String(err)), bash.path)
  }
  if (run.exitCode !== 0) {
    // `scripts/py` exits 3 with this line when no Python is found; anything else is the check failing.
    if (/no Python interpreter found/.test(run.stderr)) return failed(INSTALL_PYTHON, bash.path)

    return failed(cannotRun(lastLine(run.stderr) || `it exited with code ${run.exitCode}.`), bash.path)
  }
  let out: Resolved
  try {
    out = JSON.parse(run.stdout) as Resolved
  } catch {
    return failed(cannotRun('it printed something other than its report.'), bash.path)
  }

  return {
    profiles: out.data?.profiles ?? [],
    active: out.data?.profile ?? null,
    python: pythonOf(out),
    os: osOf(out.data?.platform),
    artifactsDir: out.data?.artifacts_dir ?? null,
    bash: bash.path,
  }
}
