import { spawn } from "node:child_process"
import path from "node:path"
import { fileURLToPath } from "node:url"
import { Plugin } from "@opencode/plugin"
import { editedPythonPaths } from "./edited-paths.mjs"

const pluginDir = path.dirname(fileURLToPath(import.meta.url))
const EDIT_TOOLS = new Set(["edit", "write", "patch"])

function run(command, args, cwd, input, timeout) {
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
    const timer = timeout && setTimeout(() => {
      child.kill("SIGKILL")
      resolve({ code: 1, stdout, stderr })
    }, timeout)
    const finish = (code) => {
      if (timer) clearTimeout(timer)
      resolve({ code: code ?? 1, stdout, stderr })
    }
    child.on("error", () => finish(1))
    child.on("close", finish)
    child.stdin.on("error", () => finish(1))
    if (input !== undefined) child.stdin.end(input)
    else child.stdin.end()
  })
}

async function guardCommand(command, cwd) {
  const unavailable = { decision: "deny", reason: "shell guard could not verify this command" }
  const result = await run(
    "python3",
    [path.join(pluginDir, "guard_bash.py")],
    cwd,
    JSON.stringify({ command, cwd }),
    8000,
  )
  if (result.code !== 0) return unavailable
  try {
    const parsed = JSON.parse(result.stdout)
    if (parsed?.decision === "allow") return null
    if ((parsed?.decision === "deny" || parsed?.decision === "ask") &&
        typeof parsed.reason === "string" && parsed.reason) return parsed
  } catch {
    return unavailable
  }
  return unavailable
}

export default Plugin.define({
  id: "project-guard",
  async setup(ctx) {
    const root = ctx.location.project?.canonical ?? ctx.location.directory

    await ctx.permission.hook("evaluate", async (event) => {
      if (event.action !== "shell" || event.effect === "deny") return
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
