import assert from "node:assert/strict"
import path from "node:path"
import test from "node:test"
import { editedPythonPaths } from "../../.opencode/plugins/project-guard/edited-paths.mjs"

const root = path.resolve("/tmp/project-guard-fixture")
const paths = (...names) => names.map((name) => path.join(root, name))

test("edit and write paths, aliases, and legacy file lists remain supported", () => {
  assert.deepEqual(
    editedPythonPaths({
      path: "app/main.py",
      filePath: "app/types.pyi",
      file_path: "app/../app/main.py",
      files: ["tests/test_main.py", { path: "app/other.py" }, {}, null],
    }, root),
    paths("app/types.pyi", "app/main.py", "tests/test_main.py", "app/other.py"),
  )
})

test("structured patches lint additions and updates but not deleted files", () => {
  const patchText = [
    "*** Begin Patch",
    "*** Add File: app/new.py",
    "+value = 1",
    "*** Update File: app/main.py",
    "@@",
    "-old",
    "+new",
    "*** Delete File: app/removed.py",
    "*** Add File: README.md",
    "+docs",
    "*** End Patch",
  ].join("\n")
  assert.deepEqual(editedPythonPaths({ patchText }, root), paths("app/new.py", "app/main.py"))
})

test("freeform CRLF patches lint only the destination of a rename", () => {
  const patch = [
    "*** Begin Patch",
    "*** Update File: app/old.py",
    "*** Move to: app/new.py",
    "@@",
    "-old",
    "+new",
    "*** Update File: app/old_stub.py",
    "*** Move to: app/new_stub.pyi",
    "*** End Patch",
  ].join("\r\n")
  assert.deepEqual(editedPythonPaths(patch, root), paths("app/new.py", "app/new_stub.pyi"))
})

test("deleted-only patches and moves away from Python do not run lint", () => {
  assert.deepEqual(editedPythonPaths({ patchText: "*** Delete File: app/old.py" }, root), [])
  assert.deepEqual(
    editedPythonPaths("*** Update File: app/old.py\n*** Move to: notes.txt", root),
    [],
  )
})

test("paths outside the checkout and non-Python paths are excluded", () => {
  assert.deepEqual(
    editedPythonPaths({ files: ["../outside.py", `${root}-other/file.py`, "README.md", root] }, root),
    [],
  )
})

test("absolute and relative duplicates are linted once", () => {
  assert.deepEqual(
    editedPythonPaths({ path: "app/main.py", files: [path.join(root, "app/main.py")] }, root),
    paths("app/main.py"),
  )
})

test("absent or unrelated tool inputs are ignored", () => {
  for (const input of [undefined, null, 3, {}, { patchText: 4 }, { files: "app/main.py" }]) {
    assert.deepEqual(editedPythonPaths(input, root), [])
  }
})
