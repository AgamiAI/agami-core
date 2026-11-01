// The agami pane's state contract: what the screens read and write through `$.state`, keyed by the
// plugin's name. Each screen task widens it rather than declaring state of its own.

export type AgamiScreen = 'home' | 'connect' | 'first-answer'

export type AgamiConnectState = {
  /**
   * `store`: the OS store's library is missing and `sm install` would add it; `driver`: the database
   * driver is missing and pip would add `package`; `installing`: one of those is running.
   */
  phase: 'form' | 'waiting' | 'connected' | 'failed' | 'manual' | 'store' | 'driver' | 'installing'
  profile?: string
  /** The pip package(s) the helper said are missing, space-separated (`duckdb pytz`). */
  package?: string
  /** The last install of `package` failed, so its button says Try again. */
  installFailed?: boolean
  dbType?: string
  url?: string
  detail?: string
  /** The non-secret connection fields typed so far, by the credentials file's key. Never a password. */
  fields?: Record<string, string>
  /** `sso` for a browser sign-in (Snowflake): the helper serves no page and stores no secret. */
  auth?: 'password' | 'sso'
}

/**
 * The sample path on the home screen: whether its prompt's turn is still running (and which turn, once
 * it has started), and what to say if it ended without a model.
 */
export type AgamiSampleState = { waiting: boolean; turnId: string | null; note: string | null }

export type AgamiOs = 'mac' | 'windows' | 'other'

/** What the pane learns once per open from `connect_resolve.py`; the home screen draws from it. */
export type AgamiEnv = {
  profiles: string[]
  active: string | null
  python: { ok: boolean; fix?: string }
  os: AgamiOs
  /** Where agami keeps its files; null when the check could not run. */
  artifactsDir: string | null
  /** The bash every agami command runs through (Git Bash on Windows); null when none was found. */
  bash: string | null
}

declare module 'claude-code' {
  interface PluginState {
    'agami-core': {
      screen: AgamiScreen
      connect: Shaped<AgamiConnectState>
      sample: Shaped<AgamiSampleState>
      // null until the check on open lands.
      env: Shaped<AgamiEnv | null>
    }
  }
}
