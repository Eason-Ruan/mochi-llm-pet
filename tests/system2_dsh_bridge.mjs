import assert from 'node:assert/strict'
import {mkdtempSync, readFileSync, rmSync} from 'node:fs'
import {tmpdir} from 'node:os'
import {join} from 'node:path'
import test from 'node:test'
import {apply} from '../deskpet/system2/dsh_bridge.mjs'

test('prompt selection and native step authentication follow one admission', async t => {
  const directory = mkdtempSync(join(tmpdir(), 'mochi-bridge-test-'))
  let dispose
  t.after(() => {
    dispose?.()
    rmSync(directory, {recursive: true, force: true})
  })

  const injected = []
  const agent = {status: 'idle', session: {id: 'fixture'}, inject: message => injected.push(message)}
  const hooks = new Map()
  const sections = new Map()
  const token = 'fixture-bridge-token'
  const version = 'system2.zh.v14'
  const portFile = join(directory, 'port')
  process.env.MOCHI_BRIDGE_TOKEN = token
  await apply({
    agents: {get: id => id === agent.session.id ? agent : undefined},
    on: (event, callback) => hooks.set(event, callback),
    effect: initialize => { dispose = initialize() },
    provide() {},
    systemPrompt: {section: section => sections.set(section.name, section)},
    tools: {guard() {}},
  }, {
    origin: 'http://127.0.0.1:1', tools: [], portFile,
    promptCatalog: {version, sections: [['first', 'First rule'], ['second', 'Second rule']]},
  })

  const base = 'http://127.0.0.1:' + readFileSync(portFile, 'utf8')
  async function inject(overrides = {}, bearer = token) {
    const response = await fetch(base + '/inject', {
      method: 'POST',
      headers: {authorization: 'Bearer ' + bearer, 'content-type': 'application/json'},
      body: JSON.stringify({
        sessionId: agent.session.id, admissionId: 'fixture-admission', text: 'Untrusted snapshot data',
        toolNames: [], promptVersion: version, sectionNames: ['first'], ...overrides,
      }),
    })
    return response.status
  }
  async function preStep(turn, step, messages, next = () => ({kind: 'enter', messages})) {
    return (await hooks.get('agent/pre-step')({agent, turn, step, messages}, next)).kind
  }
  const rule = (name, subject = agent) => sections.get('mochi:' + name).text({agent: subject})

  assert.equal(await inject({}, 'invalid'), 403)
  assert.equal(await inject({promptVersion: 'system2.zh.v13'}), 400)
  assert.equal(await inject({sectionNames: ['missing']}), 400)
  assert.equal(await inject({sectionNames: ['first', 'first']}), 400)
  assert.equal(await inject({sectionNames: ['second', 'first']}), 400)
  assert.equal(await preStep(1, 1, []), 'reject')

  assert.equal(await inject(), 204)
  assert.equal(rule('first'), 'First rule')
  assert.equal(rule('second'), '')
  assert.equal(rule('first', {}), '')
  assert.equal(await inject({sectionNames: ['second']}), 400)
  assert.equal(await preStep(1, 1, []), 'reject')
  assert.equal(rule('first'), '')

  for (const invalidMarkers of [
    marker => [marker, marker],
    marker => [{...marker, id: 'other-context-message'}],
    marker => [{...marker, source: {...marker.source, admissionId: 'other-admission'}}],
  ]) {
    assert.equal(await inject(), 204)
    assert.equal(await preStep(1, 1, invalidMarkers(injected.at(-1))), 'reject')
    assert.equal(rule('first'), '')
  }

  assert.equal(await inject(), 204)
  assert.equal(await preStep(2, 1, [injected.at(-1)], () => ({kind: 'enter', messages: []})), 'reject')
  assert.equal(rule('first'), '')
  assert.equal(await inject(), 204)
  assert.equal(await preStep(2, 1, [injected.at(-1)]), 'enter')
  assert.equal(await preStep(2, 2, []), 'enter')
  assert.equal(rule('first'), 'First rule')
  assert.equal(await preStep(2, 3, [injected.at(-1)]), 'reject')

  assert.equal(await inject({sectionNames: ['second']}), 204)
  assert.equal(rule('first'), '')
  assert.equal(rule('second'), 'Second rule')
  assert.equal(await preStep(3, 1, [injected.at(-1)]), 'enter')
  assert.equal(await preStep(4, 2, []), 'reject')

  const failure = new Error('Synthetic pre-step failure')
  assert.equal(await inject(), 204)
  await assert.rejects(preStep(5, 1, [injected.at(-1)], () => { throw failure }), failure)
  assert.equal(rule('first'), '')

  for (const event of ['agent/status', 'agent/error', 'agent/disposed']) {
    assert.equal(await inject({sectionNames: ['second']}), 204)
    hooks.get(event)({agent, status: 'idle'})
    assert.equal(rule('second'), '')
    assert.equal(await preStep(6, 1, [injected.at(-1)]), 'reject')
  }
})
