import unittest, os
import ../src/lis

suite "LIS Nim Bindings":
  test "parse json document":
    let examplePath = currentSourcePath().parentDir() / "../../../../docs/examples/server-btrfs.lis.json"
    let jsonText = readFile(examplePath)
    let doc = parseDocument(jsonText)
    check doc.lis == "0.1.0"
