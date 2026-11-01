import { expect, test } from 'claude-code/testing'

const PANE = {
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

test('/agami opens the agami pane', async ($, on) => {
  // These hooks stand for the engine beneath the plugin.
  const registered: string[] = []
  const opened: { id: string; title?: string }[] = []
  on('command.register', (_, e) => {
    registered.push(e.name)
    return { value: { command: e.name } }
  })
  on('ui.open', (_, e) => {
    opened.push({ id: e.id, title: e.title })
    return { value: { isPlaced: true } }
  })
  on('session.start', (_, e) => ({ cwd: e.cwd }))

  await $.session.start({ cwd: '/', surface: 'desktop', isInteractive: true })
  // As the person typing `/agami` in a docked layout raises it.
  await $.command.run({
    command: 'agami',
    args: '',
    origin: { kind: 'composer' },
    presentation: { isFullscreen: true, columns: 160 },
  })

  expect(registered).toEqual(['agami'])
  expect(opened).toEqual([{ id: 'agami', title: 'agami' }])
})

test('the pane draws on every surface it opens on', async $ => {
  for (const surface of ['terminal', 'desktop', 'vscode'] as const) {
    const ui = await $.ui.mount({ ...PANE, surface })

    expect(await ui.find({ type: 'Text', text: /agami/ })).toBeDefined()
    await ui.unmount()
  }
})
