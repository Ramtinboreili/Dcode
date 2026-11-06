'use strict';
const test = require('node:test');
const assert = require('node:assert');
const { slugify } = require('./slug');

test('spaces become dashes', () => {
  assert.strictEqual(slugify('Hello big World'), 'hello-big-world');
});

test('punctuation goes away', () => {
  assert.strictEqual(slugify("What's new, today?"), 'whats-new-today');
});
