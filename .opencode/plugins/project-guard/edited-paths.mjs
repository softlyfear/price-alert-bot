import path from "node:path"

export function editedPythonPaths(input, cwd) {
  const names = []
  if (input && typeof input === "object") {
    for (const key of ["filePath", "file_path", "path"]) {
      if (typeof input[key] === "string") names.push(input[key])
    }
    if (Array.isArray(input.files)) {
      for (const file of input.files) {
        if (typeof file === "string") names.push(file)
        else if (file && typeof file.path === "string") names.push(file.path)
      }
    }
  }

  const patch = typeof input === "string" ? input : input?.patchText
  if (typeof patch === "string") {
    let target
    for (const line of patch.split(/\r?\n/)) {
      const header = /^\*\*\* (Add File|Update File|Delete File|Move to): (.+)$/.exec(line)
      if (!header) continue
      if (header[1] === "Move to") {
        target = header[2]
      } else {
        if (target) names.push(target)
        target = header[1] === "Delete File" ? undefined : header[2]
      }
    }
    if (target) names.push(target)
  }

  const root = path.resolve(cwd)
  return [...new Set(names.map((name) => path.resolve(root, name)))]
    .filter((file) => file.endsWith(".py") || file.endsWith(".pyi"))
    .filter((file) => file.startsWith(root + path.sep))
}
