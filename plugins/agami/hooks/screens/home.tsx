import type { ElementTable, RenderElement } from 'claude-code'

// A placeholder the home screen task replaces: the pane's name, drawn as text on every surface
// (the terminal has no `Svg`, so text is the one form all three surfaces share).
export const home = (ui: ElementTable): RenderElement => {
  const { Box, Text } = ui

  return (
    <Box flexDirection="column">
      <Text bold>agami</Text>
    </Box>
  )
}
