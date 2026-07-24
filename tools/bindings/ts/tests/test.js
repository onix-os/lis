import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { join, resolve } from 'node:path';
import { parseDocument, stringifyDocument } from '../dist/src/document.js';

test('parse and stringify example document', () => {
  const examplePath = resolve(process.cwd(), '..', '..', '..', 'docs', 'examples', 'server-btrfs.lis.json');
  const jsonText = readFileSync(examplePath, 'utf8');

  const doc = parseDocument(jsonText);
  assert.equal(doc.lis, '0.1.0');
  assert.equal(doc.system?.hostname, 'tron');

  const out = stringifyDocument(doc);
  assert.ok(out.includes('0.1.0'));
});
