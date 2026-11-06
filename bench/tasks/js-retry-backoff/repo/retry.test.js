'use strict';
const test = require('node:test');
const assert = require('node:assert');
const { withRetry } = require('./retry');

test('retries an async failure', async () => {
  let calls = 0;
  const value = await withRetry(async () => {
    calls += 1;
    if (calls < 2) throw new Error('nope');
    return 'ok';
  }, { sleep: async () => {} });
  assert.strictEqual(value, 'ok');
  assert.strictEqual(calls, 2);
});
