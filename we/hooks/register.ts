// The we plugin's one hooks module: the /flow and /board panes, and the crew pane /board opens. The engine
// takes one session.start hook per module without a matcher, so it starts all three here.
import type { Register } from 'claude-code'

import { registerCrew, startCrew } from './board/crew'
import { registerBoard, startBoard } from './board/register'
import { registerFlow, startFlow } from './flow/register'

export const register: Register = (on, options) => {
  registerFlow(on, options)
  registerBoard(on, options)
  registerCrew(on)
  on('session.start', async ($, e, next) => {
    await startFlow($)
    await startBoard($, options)
    await startCrew($)
    return next(e)
  })
}
