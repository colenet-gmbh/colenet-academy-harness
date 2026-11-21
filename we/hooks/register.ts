// The we plugin's one hooks module: the /flow and /board panes, and the Zentrale pane /board opens. The engine
// takes one session.start hook per module without a matcher, so it starts all three here.
import type { Register } from 'claude-code'

import { registerBoard, startBoard } from './board/register'
import { registerZentrale, startZentrale } from './board/zentrale-pane'
import { registerFlow, startFlow } from './flow/register'

export const register: Register = (on, options) => {
  registerFlow(on, options)
  registerBoard(on, options)
  registerZentrale(on)
  on('session.start', async ($, e, next) => {
    await startFlow($)
    await startBoard($, options)
    await startZentrale($)
    return next(e)
  })
}
