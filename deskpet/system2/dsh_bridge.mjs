import { createServer } from 'node:http'
import { randomUUID, timingSafeEqual } from 'node:crypto'
import { writeFileSync } from 'node:fs'

export const name = 'mochi-bridge'
export const inject = ['agents', 'sessions', 'tools', 'systemPrompt']

const AUTH_SOURCE = 'mochi-context'
const MAX_INPUT = 2 * 1024 * 1024

function equal(a, b) {
  const left = Buffer.from(a ?? '')
  const right = Buffer.from(b ?? '')
  return left.length === right.length && timingSafeEqual(left, right)
}

function matchesMarker(messages, binding, step) {
  const injected = messages.filter(message => message.source?.kind === AUTH_SOURCE)
  return step === 1
    ? injected.length === 1 && injected[0].role === 'user' && injected[0].id === binding.contextId
      && injected[0].source.admissionId === binding.admissionId
    : injected.length === 0
}

export async function apply(ctx, config) {
  const token = process.env.MOCHI_BRIDGE_TOKEN
  if (!token || !/^http:\/\/127\.0\.0\.1:[1-9]\d*$/.test(config.origin)) {
    throw new Error('Invalid Mochi bridge origin')
  }
  const authorization = 'Bearer ' + token
  const allowed = new Set(config.tools.map(tool => tool.name))
  const bindings = new WeakMap()
  const activeSteps = new WeakMap()
  const catalog = config.promptCatalog
  const catalogVersion = catalog?.version ?? null
  const sectionPositions = new Map()
  if (catalog && (typeof catalogVersion !== 'string' || !catalogVersion
      || !Array.isArray(catalog.sections) || catalog.sections.length === 0)) {
    throw new Error('Invalid Mochi prompt catalog')
  }
  for (const [name, text] of catalog?.sections ?? []) {
    const order = sectionPositions.size + 1
    if (!/^[a-z][a-z0-9_]*$/.test(name) || sectionPositions.has(name) || typeof text !== 'string' || !text) {
      throw new Error('Invalid Mochi prompt catalog')
    }
    sectionPositions.set(name, order)
    ctx.systemPrompt.section({
      name: 'mochi:' + name, order, interpolate: false,
      text: ({agent}) => {
        const binding = bindings.get(agent)
        return binding?.promptVersion === catalogVersion && binding.sections.has(name) ? text : ''
      },
    })
  }

  function clear(agent) {
    bindings.delete(agent)
    activeSteps.delete(agent)
  }
  ctx.on('agent/status', ({agent, status}) => { if (status === 'idle') clear(agent) })
  ctx.on('agent/error', ({agent}) => clear(agent))
  ctx.on('agent/disposed', ({agent}) => clear(agent))

  ctx.on('agent/pre-step', async ({agent, turn, step, messages}, next) => {
    const binding = bindings.get(agent)
    const previous = activeSteps.get(agent)
    if (!binding || !Number.isSafeInteger(turn) || turn < 1
        || !Number.isSafeInteger(step) || step < 1
        || (step === 1 ? previous !== undefined : (previous?.turn !== turn
          || previous.binding !== binding || step !== previous.step + 1))
        || !matchesMarker(messages, binding, step)) {
      clear(agent)
      return {kind: 'reject'}
    }
    try {
      const decision = await next()
      if (decision.kind !== 'enter' || bindings.get(agent) !== binding
          || !matchesMarker(decision.messages, binding, step)) {
        clear(agent)
        return {kind: 'reject'}
      }
      activeSteps.set(agent, {binding, turn, step})
      return decision
    } catch (error) {
      clear(agent)
      throw error
    }
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
        if (!exec.agent || !step || step.binding !== bindings.get(exec.agent) || !step.binding.tools.has(tool.name)
            || args === null || typeof args !== 'object' || Array.isArray(args)) {
          throw new Error('No admitted Mochi tool step')
        }
        const result = await fetch(config.origin + '/tool', {
          method: 'POST',
          redirect: 'error',
          headers: {authorization, 'content-type': 'application/json'},
          signal: exec.signal,
          body: JSON.stringify({
            admissionId: step.binding.admissionId, turn: step.turn, step: step.step,
            name: tool.name, args, sessionId: exec.agent.session.id, callId: exec.callId,
          }),
        })
        if (!result.ok) throw new Error('Mochi tool request rejected')
        return await result.text()
      },
    }, {when: ({agent}) => bindings.get(agent)?.tools.has(tool.name) === true})
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
      const {sessionId, admissionId, text, toolNames, promptVersion, sectionNames} =
        JSON.parse(Buffer.concat(chunks).toString('utf8'))
      const agent = typeof sessionId === 'string' && ctx.agents.get(sessionId)
      if (!agent || agent.status !== 'idle') throw new Error('Invalid Mochi session')
      if (req.url === '/checkpoint') {
        await ctx.sessions.flush(agent.session)
        status = 204
        return
      }
      if (bindings.has(agent) || typeof admissionId !== 'string' || !admissionId || typeof text !== 'string'
          || !Array.isArray(toolNames) || !toolNames.every(name => allowed.has(name))
          || promptVersion !== catalogVersion || !Array.isArray(sectionNames)
          || (catalog ? sectionNames.length === 0 : sectionNames.length !== 0)) {
        throw new Error('Invalid Mochi input')
      }
      let lastPosition = 0
      for (const name of sectionNames) {
        const position = sectionPositions.get(name)
        if (position === undefined || position <= lastPosition) throw new Error('Invalid Mochi prompt selection')
        lastPosition = position
      }
      const contextId = randomUUID()
      bindings.set(agent, Object.freeze({
        admissionId, contextId, promptVersion, tools: new Set(toolNames), sections: new Set(sectionNames),
      }))
      try {
        agent.inject({
          id: contextId, role: 'user',
          content: [{type: 'text', text}],
          source: {kind: AUTH_SOURCE, admissionId},
        })
      } catch (error) {
        clear(agent)
        throw error
      }
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
