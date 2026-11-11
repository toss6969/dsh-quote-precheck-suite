import { readFile, readdir } from 'node:fs/promises'
import { fileURLToPath } from 'node:url'
import path from 'node:path'

const PROVIDER_NAME = 'dsh-quote-precheck-suite'
const SKILLS_ROOT = fileURLToPath(new URL('./skills/', import.meta.url))
// 官方随包=600、用户本地=100..500；第三方随包放 700，重名时永远让位给本地技能
const RANK = 700
const INVOCATION = { modelInvocable: true, userInvocable: true }

function parseFrontmatter(text) {
  const m = /^---\r?\n([\s\S]*?)\r?\n---/.exec(text)
  if (!m) return null
  const meta = {}
  let key = null
  for (const raw of m[1].split(/\r?\n/)) {
    if (!raw.trim()) continue
    if (/^\s{2,}/.test(raw) && key) {
      meta[key] = meta[key].replace(/\s+$/, '') + ' ' + raw.trim()
      continue
    }
    const i = raw.indexOf(':')
    if (i < 0) continue
    key = raw.slice(0, i).trim()
    meta[key] = raw.slice(i + 1).trim()
  }
  return meta
}

async function loadSkills() {
  const entries = await readdir(SKILLS_ROOT, { withFileTypes: true })
  const out = []
  for (const e of entries) {
    if (!e.isDirectory()) continue
    const file = path.join(SKILLS_ROOT, e.name, 'SKILL.md')
    const text = await readFile(file, 'utf8')
    const meta = parseFrontmatter(text)
    if (!meta || !meta.name || !meta.description) continue
    out.push({
      dir: path.join(SKILLS_ROOT, e.name),
      name: meta.name,
      description: meta.description,
      body: text,
    })
  }
  out.sort((a, b) => (a.name < b.name ? -1 : 1))
  return out
}

function candidate(s) {
  return {
    name: s.name,
    description: s.description,
    invocation: INVOCATION,
    provider: PROVIDER_NAME,
    source: 'bundled',
    resourceBase: { kind: 'directory', path: s.dir },
    rank: RANK,
    locator: s.name,
  }
}

const provider = {
  name: PROVIDER_NAME,
  async list() {
    return (await loadSkills()).map(candidate)
  },
  async get(cand) {
    const skills = await loadSkills()
    const s = skills.find((x) => x.name === cand.name)
    if (!s) return undefined
    return {
      name: s.name,
      description: s.description,
      invocation: INVOCATION,
      provider: PROVIDER_NAME,
      source: 'bundled',
      resourceBase: { kind: 'directory', path: s.dir },
      content: s.body,
    }
  },
}

export const name = 'quote-precheck-suite'
export const inject = ['skills']

export function apply(ctx) {
  ctx.skills.registerProvider(() => provider)
}
