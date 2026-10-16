/**
 * share-longimage — 把龟龟投资框架的成果文件渲染成 B 站可分享的中文精选长图。
 *
 * 设计要点
 * --------
 * * **零第三方 import**：本模块只使用 `node:` 内建模块。DSH 的 `tools` 服务通过
 *   注入拿到并直接注册一个原始 `ToolDefinition`，因此本 profile 里不需要再安装
 *   任何 `@deepseek-ai/*` 包（pnpm 的 `autoInstallPeers: false` 不会替我们装 peer，
 *   而 profile 的 node_modules 是空的）。
 * * **不在 JS 里做算术或摘录**：渲染与摘录全部在 `assets/render_longimage.py` 里，
 *   由子进程执行；本模块只负责参数解析、进程编排与结果转述。
 * * **fail-closed**：未知参数、类型不符、找不到解释器 / 渲染脚本 / 成果文件时直接
 *   抛错并说清原因，不静默降级成「看起来成功」。
 */

import { spawn } from 'node:child_process'
import { existsSync, readdirSync, statSync } from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

/** Stable Loader identity. */
export const name = 'share-longimage'

/**
 * `tools` 是硬依赖。
 *
 * Cordis 的 `inject` 只接受「服务名数组」或「服务名 → 拦截配置 的对象」两种形态，
 * 写 `{required, optional}` 会被当成两个叫 required / optional 的服务而永远 pending。
 * 所以投递卡片所需的 `sessionProjections` 不写进 inject，改为**运行时惰性获取**：
 * 工具真正被调用时整棵树已经起来了，那时 `ctx.get('sessionProjections')` 必然可读。
 */
export const inject = ['tools']

const RENDERER = fileURLToPath(new URL('../assets/render_longimage.py', import.meta.url))

/**
 * 渲染脚本的结果键 → 工具结果的键。
 *
 * 必须显式列表：脚本输出 snake_case，工具输出 camelCase，若直接按 camelCase 取
 * `raw[key]`，`longImage` 这类键会**静默变成 undefined**（早期版本就是这样把整图丢掉、
 * 只剩分P 的）。所以这里一律写出两边的名字，并由单元测试盯住。
 */
const RESULT_KEYS = [
  ['report', 'report'],
  ['kind', 'kind'],
  ['company', 'company'],
  ['code', 'code'],
  ['verdict', 'verdict'],
  ['long_image', 'longImage'],
  ['panel_dir', 'panelDir'],
  ['panels', 'panels'],
  ['width', 'width'],
  ['height', 'height'],
  ['blocks', 'blocks'],
  ['warnings', 'warnings'],
  ['html', 'html'],
  ['brief_used', 'briefUsed'],
  ['brief_fields', 'briefFields'],
]
const RESULT_MARKER = 'DSH_SHARE_LONGIMAGE_RESULT '
const THEMES = ['paper', 'ink']
const REPORT_RX = /(_分析报告|_估值报告|_报告)\.md$|^qualitative_report\.md$/

/* ------------------------------------------------------------------ config */

function positiveInt(value, fallback, label) {
  if (value === undefined || value === null || value === '') return fallback
  if (!Number.isSafeInteger(value) || value <= 0) {
    throw new TypeError(`${label} 必须是正整数，收到 ${JSON.stringify(value)}`)
  }
  return value
}

function optionalPath(value, label) {
  if (value === undefined || value === null || value === '') return null
  if (typeof value !== 'string') throw new TypeError(`${label} 必须是字符串路径`)
  return value
}

function normalizeConfig(raw) {
  const input = raw === undefined || raw === null ? {} : raw
  if (typeof input !== 'object' || Array.isArray(input)) {
    throw new TypeError('share-longimage 的 config 必须是一个对象')
  }
  const theme = input.theme === undefined || input.theme === null ? 'paper' : input.theme
  if (!THEMES.includes(theme)) {
    throw new TypeError(`theme 必须是 ${THEMES.join(' / ')} 之一，收到 ${JSON.stringify(theme)}`)
  }
  return {
    pythonPath: optionalPath(input.pythonPath, 'pythonPath'),
    chromePath: optionalPath(input.chromePath, 'chromePath'),
    width: positiveInt(input.width, 1080, 'width'),
    panelHeight: positiveInt(input.panelHeight, 1440, 'panelHeight'),
    maxLongHeight: positiveInt(input.maxLongHeight, 16000, 'maxLongHeight'),
    timeoutMs: positiveInt(input.timeoutMs, 600000, 'timeoutMs'),
    theme,
    presentDeliverables: input.presentDeliverables !== false,
  }
}

/**
 * 手写 Standard Schema 校验器：让 Loader 的 `resolveConfig` 能校验 config，
 * 又不必依赖 `@deepseek-ai/schemastery`。
 */
export const Config = {
  '~standard': {
    version: 1,
    vendor: 'dsh-share-longimage',
    validate(value) {
      try {
        return { value: normalizeConfig(value) }
      } catch (error) {
        return { issues: [{ message: error instanceof Error ? error.message : String(error), path: [] }] }
      }
    },
  },
}

/* --------------------------------------------------------------- tool schema */

const PARAMETERS = {
  type: 'object',
  additionalProperties: false,
  properties: {
    dir: {
      type: 'string',
      description:
        '个股产物目录，如 output/06862_海底捞。相对路径按会话工作目录解析。省略时用会话工作目录，或改用 code 参数按代码查找。',
    },
    code: {
      type: 'string',
      description: '股票代码，如 06862.HK / 600941。省略 dir 时用它在 output/ 下查找唯一匹配的产物目录。',
    },
    report: {
      type: 'string',
      description: '显式指定成果 markdown；默认在产物目录里自动发现（分析报告 > 估值报告 > qualitative_report）。',
    },
    brief: {
      type: 'string',
      description:
        '可选的 brief.json 路径：人工精选的标题、结论、指标、分节文案，覆盖自动抽取结果。不提供则全自动出图。',
    },
    theme: { type: 'string', enum: THEMES, description: '配色：paper=暖纸底（Claude 默认），ink=深色。' },
    width: { type: 'integer', description: '图片宽度（默认取插件配置，1080）。' },
    panelHeight: { type: 'integer', description: '分P目标高度（默认 1440）；切分只落在内容块边界，不会截断一行字。' },
    maxLongHeight: { type: 'integer', description: '整图高度上限；超过则只出分P（默认 16000）。' },
    split: { type: 'boolean', description: '是否同时输出分P图片，默认 true。' },
    longImage: { type: 'boolean', description: '是否输出整张无缝长图，默认 true。' },
    out: { type: 'string', description: '整图输出路径；默认 {公司}_{代码}_精华长图.png。' },
  },
}

const OUTPUT_SCHEMA = {
  type: 'object',
  additionalProperties: false,
  properties: {
    ok: { type: 'boolean', description: '是否至少产出了一种图片。' },
    report: { type: 'string', description: '实际读取的成果文件。' },
    kind: { type: 'string', description: '成果类型：analysis / valuation / qualitative。' },
    company: { type: 'string' },
    code: { type: 'string' },
    verdict: { type: 'string', description: '抽取到的结论词（如「不建仓」）。' },
    longImage: { type: 'string', description: '无缝长图路径。' },
    panelDir: { type: 'string', description: '分P图片目录（该目录已设为交付物）。' },
    panels: { type: 'array', items: { type: 'string' }, description: '分P图片路径，按顺序。' },
    width: { type: 'integer' },
    height: { type: 'integer', description: '整图高度（像素）。' },
    theme: { type: 'string' },
    blocks: { type: 'array', items: { type: 'string' }, description: '实际渲染出的区块顺序。' },
    warnings: { type: 'array', items: { type: 'string' }, description: '非致命提示（缺 Chrome 等）。' },
    html: { type: 'string', description: '中间 HTML（版式可复核）。' },
    briefUsed: { type: 'boolean' },
    briefFields: { type: 'array', items: { type: 'string' }, description: '被 brief 覆盖的字段。' },
    elapsedMs: { type: 'integer' },
    error: { type: 'string', description: '失败原因（ok 为 false 时）。' },
  },
  required: ['ok'],
}

/* --------------------------------------------------------------- arg helper */

function validateArgs(args) {
  if (args === null || typeof args !== 'object' || Array.isArray(args)) {
    return ['arguments must be an object']
  }
  const violations = []
  for (const [key, value] of Object.entries(args)) {
    const spec = PARAMETERS.properties[key]
    if (spec === undefined) {
      violations.push(`unknown argument "${key}"`)
      continue
    }
    if (value === undefined || value === null) continue
    if (spec.type === 'string' && typeof value !== 'string') violations.push(`"${key}" must be a string`)
    if (spec.type === 'integer' && !Number.isSafeInteger(value)) violations.push(`"${key}" must be an integer`)
    if (spec.type === 'boolean' && typeof value !== 'boolean') violations.push(`"${key}" must be a boolean`)
    if (spec.enum !== undefined && !spec.enum.includes(value)) {
      violations.push(`"${key}" must be one of ${spec.enum.join(' / ')}`)
    }
  }
  return violations
}

/* ------------------------------------------------------------ path discovery */

/** 在工作目录向上找 .venv/bin/python —— 与 AGENTS.md 的「优先 .venv」一致。 */
function findPython(configured, cwd) {
  if (configured) return configured
  if (process.env.DSH_SHARE_PYTHON) return process.env.DSH_SHARE_PYTHON
  const leaf = process.platform === 'win32'
    ? ['Scripts', 'python.exe']
    : ['bin', 'python']
  let dir = cwd
  for (let depth = 0; depth < 6 && dir; depth += 1) {
    const candidate = path.join(dir, '.venv', ...leaf)
    if (existsSync(candidate)) return candidate
    const parent = path.dirname(dir)
    if (parent === dir) break
    dir = parent
  }
  return process.platform === 'win32' ? 'python' : 'python3'
}

function listStockDirs(root) {
  if (!existsSync(root)) return []
  try {
    return readdirSync(root, { withFileTypes: true })
      .filter((entry) => entry.isDirectory() && !entry.name.startsWith('_'))
      .map((entry) => entry.name)
      .filter((entry) => /^[0-9A-Za-z.]+_/.test(entry))
  } catch {
    return []
  }
}

function resolveByCode(cwd, code) {
  const roots = [path.join(cwd, 'output'), cwd]
  for (const root of roots) {
    if (!existsSync(root)) continue
    const hits = readdirSync(root, { withFileTypes: true })
      .filter((entry) => entry.isDirectory() && entry.name.includes(code))
      .map((entry) => path.join(root, entry.name))
    if (hits.length === 1) return hits[0]
    if (hits.length > 1) {
      throw new Error(`代码 ${code} 匹配到多个产物目录，请用 dir 指定其一：${hits.join('、')}`)
    }
  }
  const known = listStockDirs(path.join(cwd, 'output'))
  const hint = known.length > 0 ? `output/ 下现有：${known.slice(0, 12).join('、')}` : 'output/ 下没有产物目录'
  throw new Error(`未找到与代码 ${code} 匹配的产物目录（已查找 output/ 与会话工作目录）；${hint}`)
}

function assertRenderableDir(dir) {
  if (!existsSync(dir)) throw new Error(`产物目录不存在：${dir}`)
  if (!statSync(dir).isDirectory()) throw new Error(`不是目录：${dir}`)
  let hasReport = false
  try {
    hasReport = readdirSync(dir).some((entry) => REPORT_RX.test(entry))
  } catch {
    hasReport = false
  }
  if (hasReport) return
  const children = listStockDirs(dir)
  if (children.length > 0) {
    throw new Error(
      `${dir} 里没有成果 markdown，但它下面有 ${children.length} 个个股产物目录；` +
        `请把 dir 指到具体个股目录（例如 ${path.join(dir, children[0])}），或用 code 参数。`,
    )
  }
  throw new Error(
    `${dir} 里没有找到成果 markdown（匹配 _分析报告.md / _估值报告.md / qualitative_report.md）。`,
  )
}

/* ------------------------------------------------------------- child process */

function runRenderer(python, argv, { cwd, signal, timeoutMs }) {
  return new Promise((resolve, reject) => {
    let child
    try {
      child = spawn(python, argv, { cwd, env: process.env, stdio: ['ignore', 'pipe', 'pipe'] })
    } catch (error) {
      reject(new Error(`无法启动渲染进程（${python}）：${error.message}`))
      return
    }
    let stdout = ''
    let stderr = ''
    let done = false
    let cancelled = false

    const finish = (fn, value) => {
      if (done) return
      done = true
      clearTimeout(timer)
      if (signal) signal.removeEventListener('abort', onAbort)
      fn(value)
    }
    const kill = () => {
      try {
        child.kill('SIGKILL')
      } catch {
        /* 进程可能已退出 */
      }
    }
    const onAbort = () => {
      cancelled = true
      kill()
    }
    const timer = setTimeout(() => {
      kill()
      finish(reject, new Error(`渲染超时（${timeoutMs}ms）：${python} ${argv.join(' ')}`))
    }, timeoutMs)

    if (signal) {
      if (signal.aborted) onAbort()
      else signal.addEventListener('abort', onAbort, { once: true })
    }
    child.stdout.on('data', (chunk) => {
      stdout += chunk
    })
    child.stderr.on('data', (chunk) => {
      stderr += chunk
    })
    child.on('error', (error) => {
      finish(reject, new Error(`无法启动渲染进程（${python}）：${error.message}`))
    })
    child.on('close', (code) => {
      if (cancelled) {
        finish(reject, new Error('渲染已取消'))
        return
      }
      if (code !== 0) {
        const tail = stderr.trim().slice(-1600)
        finish(reject, new Error(`渲染进程以退出码 ${code} 结束。${tail ? `\n${tail}` : ''}`))
        return
      }
      finish(resolve, { stdout, stderr })
    })
  })
}

function parseResult(stdout) {
  const line = stdout
    .split('\n')
    .reverse()
    .find((entry) => entry.startsWith(RESULT_MARKER))
  if (line === undefined) {
    throw new Error(`渲染进程没有输出结果行（${RESULT_MARKER.trim()}）。原始输出：${stdout.trim().slice(-800)}`)
  }
  try {
    return JSON.parse(line.slice(RESULT_MARKER.length))
  } catch (error) {
    throw new Error(`渲染结果不是合法 JSON：${error.message}`)
  }
}

/* ------------------------------------------------------------------- render */

function summarize(value) {
  const lines = []
  const title = [value.company, value.code].filter(Boolean).join(' ')
  lines.push(
    value.ok
      ? `已生成精华长图${title ? `：${title}` : ''}${value.verdict ? `（结论：${value.verdict}）` : ''}`
      : `渲染未产出图片${title ? `：${title}` : ''}`,
  )
  if (value.longImage) lines.push(`整图 ${value.width}×${value.height}：${value.longImage}`)
  if (Array.isArray(value.panels) && value.panels.length > 0) {
    lines.push(`分P ${value.panels.length} 张（${value.panelDir}）：`)
    for (const panel of value.panels) lines.push(`  · ${path.basename(panel)}`)
  }
  if (Array.isArray(value.blocks) && value.blocks.length > 0) lines.push(`区块：${value.blocks.join(' → ')}`)
  if (value.briefUsed) lines.push(`文案覆盖：${(value.briefFields ?? []).join('、') || 'brief.json'}`)
  if (value.html) lines.push(`版式可复核 HTML：${value.html}`)
  for (const warning of value.warnings ?? []) lines.push(`注意：${warning}`)
  lines.push('图片内容全部摘录自已审计的成果文件，渲染过程不新增任何计算或判断。')
  return lines.join('\n')
}

/* -------------------------------------------------------------------- apply */

/**
 * 注册 share_longimage 工具。
 * @param ctx - 宿主上下文（需要 `tools`）。
 * @param config - 已由 {@link Config} 校验的配置。
 */
export function apply(ctx, config) {
  const settings = normalizeConfig(config)
  const pendingDeliveries = new WeakMap()

  // 监听器常驻；没有登记过投递的调用直接命中 WeakMap 未命中分支，开销可忽略。
  ctx.on('tools/result', (exec, result) => {
    const delivery = pendingDeliveries.get(exec)
    pendingDeliveries.delete(exec)
    if (delivery === undefined || result.isError || !settings.presentDeliverables) return
    try {
      delivery.session.append('deliverables/presented', {
        turn: delivery.turn,
        callId: exec.callId,
        files: delivery.files,
      })
    } catch {
      /* 投递卡片是增益，不是渲染的成败条件 */
    }
  })

  ctx.tools.register({
    name: 'share_longimage',
    description:
      '把本项目某个个股产物目录（output/{代码}_{公司}/）里已审计的成果文件，渲染成一张适合分享到 B 站的中文精选长图，' +
      '并按内容块边界切好分P图片。版式为 Claude 文档风格（暖纸底、衬线大标题、发丝分隔线、克制陶土色强调），' +
      '字号按手机看长图定标以保证中文可读性。内容由 Python 从报告与 sidecar 文件机械摘录，不做任何新计算；' +
      '可用 brief.json 覆盖标题与解说词。缺 Chrome 时不伪造图片，会如实报错并保留 HTML。',
    parameters: PARAMETERS,
    output: {
      schema: OUTPUT_SCHEMA,
      render(_args, value) {
        return [{ type: 'text', text: summarize(value) }]
      },
    },
    async execute(args, exec) {
      const violations = validateArgs(args)
      if (violations.length > 0) {
        throw new Error(`share_longimage 参数不合法：${violations.join('; ')}`)
      }
      if (!existsSync(RENDERER)) {
        throw new Error(`渲染脚本缺失：${RENDERER}（插件包可能未完整安装）`)
      }
      const cwd = exec.agent?.session?.header?.cwd ?? process.cwd()
      const started = Date.now()

      // 纯参数层的自相矛盾先判掉，不必先去碰文件系统
      const emitLong = args.longImage !== false
      const emitPanels = args.split !== false
      if (!emitLong && !emitPanels) {
        throw new Error('longImage 与 split 不能同时为 false：那样不会产出任何图片')
      }

      const theme = args.theme ?? settings.theme
      const width = args.width ?? settings.width
      const panelHeight = args.panelHeight ?? settings.panelHeight
      const maxLongHeight = args.maxLongHeight ?? settings.maxLongHeight

      const targetDir = args.dir !== undefined
        ? path.resolve(cwd, args.dir)
        : args.code !== undefined
          ? resolveByCode(cwd, args.code)
          : cwd
      assertRenderableDir(targetDir)

      const argv = [
        RENDERER,
        '--dir', targetDir,
        '--json',
        '--theme', theme,
        '--width', String(width),
        '--panel-height', String(panelHeight),
        '--max-long-height', String(maxLongHeight),
      ]
      if (emitLong && !emitPanels) argv.push('--long-only')
      if (!emitLong && emitPanels) argv.push('--panels-only')
      if (args.report !== undefined) argv.push('--report', path.resolve(cwd, args.report))
      if (args.brief !== undefined) argv.push('--brief', path.resolve(cwd, args.brief))
      if (args.out !== undefined) argv.push('--out', path.resolve(cwd, args.out))
      if (settings.chromePath) argv.push('--chrome', settings.chromePath)

      const python = findPython(settings.pythonPath, cwd)
      const { stdout, stderr } = await runRenderer(python, argv, {
        cwd: targetDir,
        signal: exec.signal,
        timeoutMs: settings.timeoutMs,
      })
      const raw = parseResult(stdout)
      if (raw.error !== undefined) throw new Error(`渲染失败：${raw.error}`)

      const value = { ok: Boolean(raw.ok) }
      for (const [from, to] of RESULT_KEYS) {
        const entry = raw[from]
        if (entry === undefined || entry === null) continue
        value[to] = entry
      }
      value.theme = theme
      value.elapsedMs = Date.now() - started
      if (value.ok !== true) {
        value.error = '未产出任何图片'
      }
      if (stderr.trim() && Array.isArray(value.warnings)) {
        const tail = stderr.trim().slice(-400)
        if (tail) value.warnings.push(tail)
      }

      const files = []
      if (typeof value.longImage === 'string') {
        files.push({ path: value.longImage, description: '精华版长图（B 站分享用）' })
      }
      if (files.length === 0 && Array.isArray(value.panels) && value.panels.length > 0) {
        files.push({ path: value.panels[0], description: '精华长图 · 分P 第 1 张' })
      }
      const sessionProjections = ctx.get('sessionProjections')
      if (files.length > 0 && settings.presentDeliverables && sessionProjections !== undefined && exec.agent) {
        try {
          const boundary = sessionProjections.stateOf(exec.agent.session, 'turnBoundary')
          if (boundary !== undefined && boundary.openTurnStartSeq !== null) {
            pendingDeliveries.set(exec, {
              session: exec.agent.session,
              turn: boundary.lastTurn,
              files,
            })
          }
        } catch {
          /* 没有打开的 turn 就不登记投递卡片 */
        }
      }
      return value
    },
  })
}
