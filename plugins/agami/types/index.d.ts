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

declare module 'claude-code' {
  interface PluginState {
    'agami-core': {
      screen: AgamiScreen
      connect: Shaped<AgamiConnectState>
    }
  }
}
