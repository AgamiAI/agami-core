import { atom, read, update } from 'claude-code'
import type { Register } from 'claude-code'

import type { AgamiEnv, AgamiScreen } from '../types'
import { readEnv } from './env'
import { connect, resetConnect } from './screens/connect'
import { home } from './screens/home'

// The pane's id and title are `agami`, the same word as the command that opens it: one name for the
// person to learn. The command's name is written out where it is registered and matched, because
// `claude plugin validate` only counts a literal there as this module answering its own command.
const PANE = 'agami'

// Declared in each file that reads or writes them: the engine lists the values a module touches by
// reading the atom where it is used, so an imported atom would not be seen.
const screenState = atom({ plugin: 'agami-core', key: 'screen' } as const, 'home' as AgamiScreen)
const envState = atom({ plugin: 'agami-core', key: 'env' } as const, null as AgamiEnv | null, {
  shape: 'env-2',
})

export const register: Register = on => {
  on('session.start', async ($, e, next) => {
    await $.command.register({
      name: 'agami',
      description: 'Open the agami pane: connect a database, try the sample, ask a question',
    })

    return next(e)
  })

  on('command.run', { command: 'agami' }, async $ => {
    await $.ui.open({ id: PANE, title: PANE })
    // Each open starts at home and checks the setup afresh, once. A render has a 10 s budget, so the
    // check runs here, not in the render: the pane draws "Checking your setup…" until it lands.
    await resetConnect($)
    await update($, screenState, () => 'home')
    await update($, envState, () => null)
    const env = await readEnv($)
    await update($, envState, () => env)

    return { text: 'agami pane opened.' }
  })

  // Closing the pane ends a helper still waiting for a password, so no page stays open behind it.
  // The close itself always goes on, whatever the cleanup did.
  on('ui.close', async ($, e, next) => {
    if (e.id === PANE) {
      await resetConnect($)
      await update($, screenState, () => 'home')
    }

    return next(e)
  }).catch(($, e, next) => next(e))

  on('ui.render', { component: 'Pane', requestId: PANE }, async ($, e) => {
    if ((await read($, screenState)) === 'connect') return connect($, e)

    return home($, e)
  })
}
