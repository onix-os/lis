"use strict";
Object.defineProperty(exports, "__esModule", { value: true });
exports.VERSION = void 0;
exports.parseDocument = parseDocument;
exports.stringifyDocument = stringifyDocument;
exports.VERSION = "0.1.0";
function parseDocument(jsonText) {
    const doc = JSON.parse(jsonText);
    if (!doc.lis) {
        throw new Error("Missing required 'lis' version field");
    }
    return doc;
}
function stringifyDocument(doc) {
    return JSON.stringify(doc, null, 2);
}
