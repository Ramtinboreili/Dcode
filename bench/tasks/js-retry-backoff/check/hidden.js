'use strict';
const assert = require('node:assert');
const path = require('node:path');
const { withRetry } = require(path.join(process.cwd(), 'retry.js'));

(async () => {
  const delays = [];
  const sleep = async (ms) => { delays.push(ms); };

  let calls = 0;
  await assert.rejects(withRetry(async () => { calls += 1; throw new Error(`boom ${calls}`); }, { maxAttempts: 3, baseDelayMs: 10, sleep }), /boom 3/);
  assert.strictEqual(calls, 3, 'maxAttempts is the total');
  assert.deepStrictEqual(delays, [10, 20], 'no sleep after the last attempt, delay doubles');

  calls = 0;
  await assert.rejects(withRetry(() => { calls += 1; throw new Error('sync'); }, { maxAttempts: 2, sleep }), /sync/);
  assert.strictEqual(calls, 2, 'sync throws are retried');

  calls = 0;
  delays.length = 0;
  await assert.rejects(withRetry(async () => { calls += 1; throw new Error('fatal'); }, { maxAttempts: 5, sleep, shouldRetry: (e) => e.message !== 'fatal' }), /fatal/);
  assert.strictEqual(calls, 1, 'shouldRetry is called with the error');
  assert.deepStrictEqual(delays, []);

  let slept = false;
  calls = 0;
  const value = await withRetry(async () => { calls += 1; if (calls === 1) throw new Error('x'); return slept; }, {
    sleep: async () => { await new Promise((r) => setTimeout(r, 5)); slept = true; },
  });
  assert.strictEqual(value, true, 'the sleep is waited for');
  assert.strictEqual(await withRetry(async () => 5), 5);
  console.log('ok');
})().catch((err) => { console.error(err); process.exit(1); });
