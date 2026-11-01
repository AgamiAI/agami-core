import type { CoreEngineInterface } from 'claude-code'

import type { AgamiEnv, AgamiOs } from '../types'

type Engine = Pick<CoreEngineInterface, 'plugin' | 'process' | 'state'>

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

function osOf(platform: string | undefined): AgamiOs {
  return platform === 'Windows' ? 'windows' : platform === 'Darwin' ? 'mac' : 'other'
}

// `connect_resolve.py` could not run at all (no bash, no Python), so it never said which system this
// is. `uname` is on macOS and Linux; where it is missing the machine is almost surely Windows.
async function osFromUname($: Engine): Promise<AgamiOs> {
  try {
    const { stdout } = await $.process.run(['uname'])

    return osOf(stdout.trim())
  } catch {
    return 'windows'
  }
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
  const script = `${$.plugin.root}/scripts/py`
  try {
    const run = await $.process.run(['bash', script, 'connect_resolve.py'], { timeoutMs: 60_000 })
    if (run.exitCode !== 0) {
      return { profiles: [], active: null, python: { ok: false, fix: INSTALL_PYTHON }, os: await osFromUname($), artifactsDir: null }
    }
    const out = JSON.parse(run.stdout) as Resolved

    return {
      profiles: out.data?.profiles ?? [],
      active: out.data?.profile ?? null,
      python: pythonOf(out),
      os: osOf(out.data?.platform),
      artifactsDir: out.data?.artifacts_dir ?? null,
    }
  } catch {
    return { profiles: [], active: null, python: { ok: false, fix: INSTALL_PYTHON }, os: await osFromUname($), artifactsDir: null }
  }
}
