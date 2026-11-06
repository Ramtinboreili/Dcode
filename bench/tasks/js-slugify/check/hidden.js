'use strict';
const assert = require('node:assert');
const path = require('node:path');
const { slugify } = require(path.join(process.cwd(), 'slug.js'));

assert.strictEqual(slugify('Hello big World'), 'hello-big-world');
assert.strictEqual(slugify('a  b   c'), 'a-b-c');
assert.strictEqual(slugify('  trim me  '), 'trim-me');
assert.strictEqual(slugify("What's new, today?"), 'whats-new-today');
assert.strictEqual(slugify('v1.2 release!'.replace('.', ' ')), 'v1-2-release');
assert.strictEqual(slugify('--already-dashed--'), 'already-dashed');
assert.strictEqual(slugify('100% done'), '100-done');
console.log('ok');
