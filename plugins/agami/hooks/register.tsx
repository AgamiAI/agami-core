import type { Register } from 'claude-code'

import { home } from './screens/home'

// The pane's id and title are `agami`, the same word as the command that opens it: one name for the
// person to learn. The command's name is written out where it is registered and matched, because
// `claude plugin validate` only counts a literal there as this module answering its own command.
const PANE = 'agami'

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

    return { text: 'agami pane opened.' }
  })

  on('ui.render', { component: 'Pane', requestId: PANE }, async ($, e) => home($.ui.resolve(e)))
}
