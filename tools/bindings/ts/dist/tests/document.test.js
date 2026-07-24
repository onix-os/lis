"use strict";
var __importDefault = (this && this.__importDefault) || function (mod) {
    return (mod && mod.__esModule) ? mod : { "default": mod };
};
Object.defineProperty(exports, "__esModule", { value: true });
const node_test_1 = require("node:test");
const strict_1 = __importDefault(require("node:assert/strict"));
const node_fs_1 = require("node:fs");
const node_path_1 = require("node:path");
const document_js_1 = require("../src/document.js");
(0, node_test_1.test)('parse and stringify example document', () => {
    const examplePath = (0, node_path_1.join)(process.cwd(), '..', '..', '..', 'docs', 'examples', 'server-btrfs.lis.json');
    const jsonText = (0, node_fs_1.readFileSync)(examplePath, 'utf8');
    const doc = (0, document_js_1.parseDocument)(jsonText);
    strict_1.default.equal(doc.lis, '0.1.0');
    strict_1.default.equal(doc.system?.hostname, 'tron');
    const out = (0, document_js_1.stringifyDocument)(doc);
    strict_1.default.ok(out.includes('0.1.0'));
});
