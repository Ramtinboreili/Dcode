'use strict';
const assert = require('node:assert');
const path = require('node:path');
const { mapLimit, mapSettled } = require(path.join(process.cwd(), 'pool.js'));

const tick = (ms = 1) => new Promise((resolve) => setTimeout(resolve, ms));

(async () => {
  let running = 0;
  let peak = 0;
  const work = async (x) => {
    running += 1;
    peak = Math.max(peak, running);
    await tick(x % 3 === 0 ? 5 : 1);
    running -= 1;
    return x * 2;
  };
  const input = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10];
  assert.deepStrictEqual(await mapLimit(input, 3, work), input.map((x) => x * 2), 'input order');
  assert.strictEqual(peak, 3, 'runs up to the limit, and no more');

  assert.deepStrictEqual(await mapLimit([], 2, work), []);
  assert.deepStrictEqual(await mapLimit([1, 2], 1, async (x, i) => [x, i]), [[1, 0], [2, 1]], 'passes the index');
  assert.deepStrictEqual(await mapLimit([1, 2], 5, (x) => x + 1), [2, 3], 'plain return values work');

  const started = [];
  await assert.rejects(mapLimit([1, 2, 3, 4, 5], 2, async (x) => {
    started.push(x);
    await tick();
    if (x === 2) throw new Error('bad 2');
    return x;
  }), /bad 2/);
  await tick(20);
  assert.ok(started.length <= 3, `stopped starting work: ${started}`);

  await assert.rejects(mapLimit([1], 1, () => { throw new Error('sync bad'); }), /sync bad/);

  const settled = await mapSettled([1, 2, 3], 2, async (x) => {
    if (x === 2) throw new Error('two');
    return x * 10;
  });
  assert.deepStrictEqual(settled[0], { status: 'fulfilled', value: 10 });
  assert.strictEqual(settled[1].status, 'rejected');
  assert.strictEqual(settled[1].reason.message, 'two');
  assert.deepStrictEqual(settled[2], { status: 'fulfilled', value: 30 });
  console.log('ok');
})().catch((err) => { console.error(err); process.exit(1); });
