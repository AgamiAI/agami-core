// The agami pane's state contract: what the screens read and write through `$.state`, keyed by the
// plugin's name. Each screen task widens it rather than declaring state of its own.

export type AgamiScreen = 'home' | 'connect' | 'first-answer'

export type AgamiConnectState = {
  phase: 'form' | 'waiting' | 'connected' | 'failed' | 'manual'
  profile?: string
  dbType?: string
  url?: string
  detail?: string
}

export type AgamiOs = 'mac' | 'windows' | 'other'

/** What the pane learns once per open from `connect_resolve.py`; the home screen draws from it. */
export type AgamiEnv = {
  profiles: string[]
  active: string | null
  python: { ok: boolean; fix?: string }
  os: AgamiOs
}

declare module 'claude-code' {
  interface PluginState {
    'agami-core': {
      screen: AgamiScreen
      connect: Shaped<AgamiConnectState>
      // null until the check on open lands.
      env: Shaped<AgamiEnv | null>
    }
  }
}
