/**
 * share-longimage 插件的离线自检（`node --test plugins/dsh-share-longimage/tests/`）。
 *
 * 不依赖 DSH 运行时：用一个最小的 stub ctx 触发 `apply`，然后直接调用注册出来的
 * `ToolDefinition`。这里覆盖的都是「装上以后才会暴露」的契约问题：
 *   · `inject` 必须是 Cordis 接受的形态（数组或 服务名→配置 的对象）；
 *   · `parameters` / `output.schema` 必须落在 dsh-tools 强制支持的 JSON Schema 子集内；
 *   · `Config` 的 Standard Schema 校验必须 fail-closed；
 *   · 参数与路径错误必须抛出可读错误，而不是静默产出半成品。
 */

import assert from 'node:assert/strict'
import { chmodSync, mkdtempSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import path from 'node:path'
import { test } from 'node:test'

import { Config, apply, inject, name } from '../lib/host.js'

const RESULT_MARKER = 'DSH_SHARE_LONGIMAGE_RESULT '

/* --------------------------------------------------------------- schema subset */

const SCHEMA_TYPES = ['string', 'number', 'integer', 'boolean', 'null', 'object', 'array']
const ANNOTATIONS = new Set(['description', 'title', 'default', 'examples'])
const CONSTRAINTS = new Set(['type', 'oneOf', 'properties', 'required', 'additionalProperties', 'items', 'enum', 'const'])
const NESTED_ONLY = {
  properties: ['object'],
  required: ['object'],
  additionalProperties: ['object'],
  items: ['array'],
  enum: ['string', 'number', 'integer', 'boolean', 'null'],
  const: ['string', 'number', 'integer', 'boolean', 'null'],
}

/** 复刻 dsh-tools `checkSchemaNode` 的强制子集，返回违规列表。 */
function schemaViolations(schema, path = 'schema', out = []) {
  if (schema === null || typeof schema !== 'object' || Array.isArray(schema)) {
    out.push(`${path} must be a schema object`)
    return out
  }
  for (const key of Object.keys(schema)) {
    if (!CONSTRAINTS.has(key) && !ANNOTATIONS.has(key)) out.push(`${path}.${key} is not a supported keyword`)
  }
  const hasType = Object.hasOwn(schema, 'type')
  const hasOneOf = Object.hasOwn(schema, 'oneOf')
  if (hasType && hasOneOf) {
    out.push(`${path} cannot declare both type and oneOf`)
    return out
  }
  if (hasOneOf) {
    if (!Array.isArray(schema.oneOf) || schema.oneOf.length < 2) out.push(`${path}.oneOf must hold at least two schemas`)
    else schema.oneOf.forEach((entry, index) => schemaViolations(entry, `${path}.oneOf[${index}]`, out))
    return out
  }
  if (!hasType) {
    out.push(`${path} needs type or oneOf`)
    return out
  }
  if (!SCHEMA_TYPES.includes(schema.type)) {
    out.push(`${path}.type must be one of ${SCHEMA_TYPES.join('/')}`)
    return out
  }
  for (const [key, allowed] of Object.entries(NESTED_ONLY)) {
    if (Object.hasOwn(schema, key) && !allowed.includes(schema.type)) out.push(`${path}.${key} is not supported on ${schema.type}`)
  }
  if (schema.type === 'object') {
    if (Object.hasOwn(schema, 'required')) {
      if (!Array.isArray(schema.required) || schema.required.some((entry) => typeof entry !== 'string')) {
        out.push(`${path}.required must be an array of strings`)
      } else {
        for (const key of schema.required) {
          if (!schema.properties || !Object.hasOwn(schema.properties, key)) out.push(`${path}.required names "${key}" which is not in properties`)
        }
      }
    }
    if (Object.hasOwn(schema, 'additionalProperties') && typeof schema.additionalProperties !== 'boolean') {
      out.push(`${path}.additionalProperties must be a boolean`)
    }
    for (const [key, value] of Object.entries(schema.properties ?? {})) schemaViolations(value, `${path}.properties.${key}`, out)
  }
  if (schema.type === 'array' && Object.hasOwn(schema, 'items')) schemaViolations(schema.items, `${path}.items`, out)
  if (Object.hasOwn(schema, 'enum')) {
    const scalar = (entry) => {
      if (schema.type === 'string') return typeof entry === 'string'
      if (schema.type === 'integer') return Number.isSafeInteger(entry)
      if (schema.type === 'number') return typeof entry === 'number'
      if (schema.type === 'boolean') return typeof entry === 'boolean'
      return entry === null
    }
    if (!Array.isArray(schema.enum) || schema.enum.length === 0 || !schema.enum.every(scalar)) {
      out.push(`${path}.enum must be a non-empty array of ${schema.type} values`)
    }
  }
  return out
}

/* -------------------------------------------------------------------- harness */

/** 用 stub ctx 触发 apply，拿回注册出来的 ToolDefinition。 */
function register(config) {
  let definition
  const listeners = []
  const ctx = {
    get: () => undefined,
    on: (event, handler) => listeners.push({ event, handler }),
    tools: { register: (value) => { definition = value; return () => {} } },
  }
  apply(ctx, config)
  assert.ok(definition, 'apply() 必须注册工具')
  return { definition, listeners }
}

const EXEC = { callId: 'self-test', signal: new AbortController().signal, agent: undefined }

/* ---------------------------------------------------------------------- tests */

test('identity and inject', () => {
  assert.equal(name, 'share-longimage')
  assert.ok(Array.isArray(inject), 'inject 必须是服务名数组（对象形态会被 Cordis 当成服务名）')
  assert.deepEqual(inject, ['tools'])
})

test('config validates and fails closed', () => {
  const validate = (value) => Config['~standard'].validate(value)
  assert.equal(validate({}).value.width, 1080)
  assert.equal(validate({ theme: 'ink' }).value.theme, 'ink')
  assert.ok(validate({ width: 0 }).issues, 'width 0 必须被拒绝')
  assert.ok(validate({ width: 1.5 }).issues, 'width 1.5 必须被拒绝')
  assert.ok(validate({ theme: 'neon' }).issues, '未知 theme 必须被拒绝')
  assert.ok(validate({ pythonPath: 42 }).issues, 'pythonPath 非字符串必须被拒绝')
})

test('declared schemas stay inside the enforced subset', () => {
  const { definition } = register({})
  assert.equal(definition.name, 'share_longimage')
  assert.deepEqual(schemaViolations(definition.parameters), [])
  assert.deepEqual(schemaViolations(definition.output.schema), [])
  assert.equal(typeof definition.output.render, 'function')
  assert.equal(typeof definition.execute, 'function')
})

test('render projects the result into readable text', () => {
  const { definition } = register({})
  const blocks = definition.output.render({}, {
    ok: true, company: '海底捞', code: '06862.HK', verdict: '不建仓',
    width: 1080, height: 8284, longImage: '/tmp/a.png',
    panels: ['/tmp/p01.png', '/tmp/p02.png'], panelDir: '/tmp/panels',
    blocks: ['hero', 'verdict'], warnings: ['注意某事'], briefUsed: true, briefFields: ['title'],
  })
  assert.equal(blocks.length, 1)
  assert.equal(blocks[0].type, 'text')
  const text = blocks[0].text
  for (const expected of ['海底捞 06862.HK', '不建仓', '/tmp/a.png', '分P 2 张', 'p01.png', 'hero → verdict', '注意某事', 'title']) {
    assert.ok(text.includes(expected), `转述里应当出现 ${expected}`)
  }
})

test('unknown arguments are rejected', async () => {
  const { definition } = register({})
  await assert.rejects(() => definition.execute({ nope: 1 }, EXEC), /unknown argument "nope"/)
})

test('type errors are rejected', async () => {
  const { definition } = register({})
  await assert.rejects(() => definition.execute({ width: 'wide' }, EXEC), /"width" must be an integer/)
  await assert.rejects(() => definition.execute({ theme: 'neon' }, EXEC), /"theme" must be one of paper \/ ink/)
})

test('missing directory reports the resolved path', async () => {
  const { definition } = register({})
  await assert.rejects(
    () => definition.execute({ dir: 'definitely/not/here' }, EXEC),
    /产物目录不存在/,
  )
})

test('refusing to emit nothing', async () => {
  const { definition } = register({})
  await assert.rejects(
    () => definition.execute({ longImage: false, split: false }, EXEC),
    /不能同时为 false/,
  )
})

test('the tools/result listener is registered exactly once', () => {
  const { listeners } = register({})
  const results = listeners.filter((entry) => entry.event === 'tools/result')
  assert.equal(results.length, 1)
})

/**
 * 用一个假「解释器」替换渲染脚本，走完 spawn → 解析结果行 → 投影键的全链路。
 * 这一条盯的是真实事故：脚本输出 snake_case，工具输出 camelCase，曾经因为按
 * camelCase 取 `raw[key]`，把 `long_image` 静默丢成 undefined，只剩分P。
 */
test('renderer result keys are projected from snake_case to camelCase', async () => {
  const dir = mkdtempSync(path.join(tmpdir(), 'share-longimage-'))
  writeFileSync(path.join(dir, '测试公司_000001_分析报告.md'), '# 测试\n')
  const payload = {
    ok: true,
    report: 'r.md',
    long_image: '/tmp/long.png',
    panel_dir: '/tmp/panels',
    panels: ['/tmp/p01.png', '/tmp/p02.png'],
    width: 1080,
    height: 4321,
    blocks: ['hero', 'verdict'],
    warnings: ['缺 Chrome'],
    html: '/tmp/x.html',
    brief_used: true,
    brief_fields: ['title', 'kpis'],
  }
  const fake = path.join(dir, 'fake-python')
  writeFileSync(fake, `#!/bin/sh\nprintf '%s\\n' '${RESULT_MARKER}${JSON.stringify(payload)}'\n`)
  chmodSync(fake, 0o755)

  const { definition } = register({ pythonPath: fake })
  const value = await definition.execute({ dir }, EXEC)
  assert.equal(value.ok, true)
  assert.equal(value.longImage, '/tmp/long.png', 'long_image 必须映射成 longImage')
  assert.equal(value.panelDir, '/tmp/panels', 'panel_dir 必须映射成 panelDir')
  assert.equal(value.briefUsed, true, 'brief_used 必须映射成 briefUsed')
  assert.deepEqual(value.briefFields, ['title', 'kpis'])
  assert.deepEqual(value.panels, ['/tmp/p01.png', '/tmp/p02.png'])
  assert.equal(value.height, 4321)
  assert.deepEqual(value.warnings, ['缺 Chrome'])
  assert.equal(typeof value.elapsedMs, 'number')
})

test('a renderer that reports an error surfaces it as a failure', async () => {
  const dir = mkdtempSync(path.join(tmpdir(), 'share-longimage-'))
  writeFileSync(path.join(dir, '测试公司_000002_分析报告.md'), '# 测试\n')
  const fake = path.join(dir, 'fake-python')
  writeFileSync(fake, `#!/bin/sh\nprintf '%s\\n' '${RESULT_MARKER}${JSON.stringify({ ok: false, error: 'Chrome 未找到' })}'\n`)
  chmodSync(fake, 0o755)
  const { definition } = register({ pythonPath: fake })
  await assert.rejects(() => definition.execute({ dir }, EXEC), /Chrome 未找到/)
})

test('a non-zero renderer exit includes its stderr', async () => {
  const dir = mkdtempSync(path.join(tmpdir(), 'share-longimage-'))
  writeFileSync(path.join(dir, '测试公司_000003_分析报告.md'), '# 测试\n')
  const fake = path.join(dir, 'fake-python')
  writeFileSync(fake, '#!/bin/sh\necho "boom: 版式渲染失败" >&2\nexit 3\n')
  chmodSync(fake, 0o755)
  const { definition } = register({ pythonPath: fake })
  await assert.rejects(() => definition.execute({ dir }, EXEC), /退出码 3[\s\S]*boom/)
})
