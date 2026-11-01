import type { ElementTable, RenderElement } from 'claude-code'

// A placeholder the Connect screen task replaces.
export const connect = (ui: ElementTable): RenderElement => {
  const { Box, Text } = ui

  return (
    <Box flexDirection="column">
      <Text>Connect a database: this screen is not built yet.</Text>
    </Box>
  )
}
