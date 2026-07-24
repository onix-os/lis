import { parseDocument, stringifyDocument } from '../src/document.ts';

if (typeof Deno !== 'undefined') {
  Deno.test('parse and stringify example document (Deno)', async () => {
    const examplePath = new URL('../../../../docs/examples/server-btrfs.lis.json', import.meta.url);
    const jsonText = await Deno.readTextFile(examplePath);

    const doc = parseDocument(jsonText);
    if (doc.lis !== '0.1.0') throw new Error('Expected 0.1.0');
    if (doc.system?.hostname !== 'tron') throw new Error('Expected hostname tron');

    const out = stringifyDocument(doc);
    if (!out.includes('0.1.0')) throw new Error('Serialized JSON missing version');
  });
}

if (typeof process !== 'undefined' && (process as any).isBun) {
  const { test, expect } = require('bun:test');
  const { readFileSync } = require('fs');
  const { resolve } = require('path');

  test('parse and stringify example document (Bun)', () => {
    const examplePath = resolve(__dirname, '../../../../docs/examples/server-btrfs.lis.json');
    const jsonText = readFileSync(examplePath, 'utf8');

    const doc = parseDocument(jsonText);
    expect(doc.lis).toBe('0.1.0');
    expect(doc.system?.hostname).toBe('tron');

    const out = stringifyDocument(doc);
    expect(out).toContain('0.1.0');
  });
}
