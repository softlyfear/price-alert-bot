import { spawn } from "node:child_process"
import path from "node:path"
import { fileURLToPath } from "node:url"
import { Plugin } from "@opencode/plugin"
import { editedPythonPaths } from "./edited-paths.mjs"

const pluginDir = path.dirname(fileURLToPath(import.meta.url))
const EDIT_TOOLS = new Set(["edit", "write", "patch"])

function run(command, args, cwd, input) {
  return new Promise((resolve) => {
    const child = spawn(command, args, { cwd })
    let stdout = ""
    let stderr = ""
    child.stdout.on("data", (chunk) => {
      stdout += chunk
    })
    child.stderr.on("data", (chunk) => {
      stderr += chunk
    })
    child.on("error", () => resolve({ code: 1, stdout, stderr }))
    child.on("close", (code) => resolve({ code: code ?? 1, stdout, stderr }))
    if (input !== undefined) child.stdin.end(input)
    else child.stdin.end()
  })
}

async function guardCommand(command, cwd) {
  const result = await run(
    "python3",
    [path.join(pluginDir, "guard_bash.py")],
    cwd,
    JSON.stringify({ command, cwd }),
  )
  if (result.code !== 0) return null
  try {
    const parsed = JSON.parse(result.stdout)
    if (parsed && (parsed.decision === "deny" || parsed.decision === "ask")) return parsed
  } catch {
    return null
  }
  return null
}

export default Plugin.define({
  id: "project-guard",
  async setup(ctx) {
    const root = ctx.location.project?.canonical ?? ctx.location.directory

    await ctx.permission.hook("evaluate", async (event) => {
      if (event.action !== "shell") return
      for (const command of event.resources) {
        if (typeof command !== "string" || command.length === 0) continue
        const verdict = await guardCommand(command, root)
        if (!verdict) continue
        if (verdict.decision === "deny") {
          event.effect = "deny"
          event.message = `guard: ${verdict.reason}`
          return
        }
        if (verdict.decision === "ask" && event.effect !== "deny") {
          event.effect = "ask"
          event.message = `guard: ${verdict.reason}`
        }
      }
    })

    await ctx.tool.hook("execute.after", async (event) => {
      if (event.status !== "completed" || !EDIT_TOOLS.has(event.tool)) return
      const paths = editedPythonPaths(event.input, root)
      if (paths.length === 0) return
      const check = await run(
        "uv",
        ["run", "--quiet", "ruff", "check", "--output-format=concise", "--ignore", "F401,F841", "--", ...paths],
        root,
      )
      if (check.code === 0) return
      const lint = (check.stdout || check.stderr).trim()
      if (!lint) return
      const note = `ruff check: ${lint}`
      if (event.result && Array.isArray(event.result.content)) {
        event.result.content.push({ type: "text", text: note })
      } else if (event.result && typeof event.result === "object") {
        event.result.metadata = { ...(event.result.metadata ?? {}), ruff: lint }
      }
    })
  },
})
