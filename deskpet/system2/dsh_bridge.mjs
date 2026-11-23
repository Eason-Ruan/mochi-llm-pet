import { createServer } from 'node:http'
import { randomUUID, timingSafeEqual } from 'node:crypto'
import { writeFileSync } from 'node:fs'

export const name = 'mochi-bridge'
export const inject = ['agents', 'sessions', 'tools']

const AUTH_SOURCE = 'mochi-context'
const MAX_INPUT = 2 * 1024 * 1024

function equal(a, b) {
  const left = Buffer.from(a ?? '')
  const right = Buffer.from(b ?? '')
  return left.length === right.length && timingSafeEqual(left, right)
}

export async function apply(ctx, config) {
  const token = process.env.MOCHI_BRIDGE_TOKEN
  if (!token || !/^http:\/\/127\.0\.0\.1:[1-9]\d*$/.test(config.origin)) {
    throw new Error('Invalid Mochi bridge origin')
  }
  const authorization = 'Bearer ' + token
  const allowed = new Set(config.tools.map(tool => tool.name))
  const activeSteps = new WeakMap()
  const admittedTools = new WeakMap()
  ctx.on('agent/pre-step', async ({agent, turn, step, messages}, next) => {
    const injected = messages.filter(message => message.source?.kind === AUTH_SOURCE)
    if (injected.length === 1) {
      activeSteps.set(agent, {admissionId: injected[0].source.admissionId, turn, step})
    } else {
      const previous = activeSteps.get(agent)
      if (injected.length === 0 && previous?.turn === turn) {
        activeSteps.set(agent, {...previous, step})
      } else {
        activeSteps.delete(agent)
      }
    }
    return next()
  })

  for (const tool of config.tools) {
    ctx.tools.register({
      name: tool.name,
      description: tool.description,
      parameters: tool.parameters,
      output: {
        schema: {type: 'string'},
        render(_args, value) { return [{type: 'text', text: value}] },
      },
      async execute(args, exec) {
        const step = activeSteps.get(exec.agent)
        if (!exec.agent || !step || args === null || typeof args !== 'object' || Array.isArray(args)) {
          throw new Error('No admitted Mochi tool step')
        }
        const result = await fetch(config.origin + '/tool', {
          method: 'POST',
          redirect: 'error',
          headers: {authorization, 'content-type': 'application/json'},
          signal: exec.signal,
          body: JSON.stringify({
            ...step, name: tool.name, args, sessionId: exec.agent.session.id, callId: exec.callId,
          }),
        })
        if (!result.ok) throw new Error('Mochi tool request rejected')
        return await result.text()
      },
    }, {when: ({agent}) => admittedTools.get(agent)?.has(tool.name) === true})
  }
  ctx.tools.guard(({name}) => allowed.has(name) ? undefined : 'Tool is outside the Mochi capability allowlist')

  // The SDK needs a workspace service. Mochi uses a fixed directory without a model-facing prompt.
  ctx.provide('workingDirectory', {
    async ensure(agent) { return agent.session.header.cwd ?? process.cwd() },
    async set() { throw new Error('Mochi does not change working directory') },
  })

  const server = createServer(async (req, res) => {
    let status = 400
    try {
      if (!['/inject', '/checkpoint'].includes(req.url) || req.method !== 'POST' || !equal(req.headers.authorization, authorization)) {
        status = 403
        return
      }
      let size = 0
      const chunks = []
      for await (const chunk of req) {
        size += chunk.length
        if (size > MAX_INPUT) throw new Error('Mochi input exceeded limit')
        chunks.push(chunk)
      }
      const {sessionId, admissionId, text, toolNames} = JSON.parse(Buffer.concat(chunks).toString('utf8'))
      const agent = typeof sessionId === 'string' && ctx.agents.get(sessionId)
      if (!agent || agent.status !== 'idle') throw new Error('Invalid Mochi session')
      if (req.url === '/checkpoint') {
        await ctx.sessions.flush(agent.session)
        status = 204
        return
      }
      if (typeof admissionId !== 'string' || typeof text !== 'string'
          || !Array.isArray(toolNames) || !toolNames.every(name => allowed.has(name))) {
        throw new Error('Invalid Mochi input')
      }
      admittedTools.set(agent, new Set(toolNames))
      agent.inject({
        id: randomUUID(), role: 'user',
        content: [{type: 'text', text}],
        source: {kind: AUTH_SOURCE, admissionId},
      })
      status = 204
    } catch {
      status = 400
    } finally {
      res.writeHead(status).end()
    }
  })
  server.requestTimeout = 5000
  await new Promise((resolve, reject) => {
    server.once('error', reject)
    server.listen(0, '127.0.0.1', resolve)
  })
  writeFileSync(config.portFile, String(server.address().port), {mode: 0o600, flag: 'wx'})
  ctx.effect(() => () => { server.close(); server.closeAllConnections() })
}
