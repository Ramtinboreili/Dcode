'use strict';

const realSleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

async function withRetry(fn, { maxAttempts = 3, baseDelayMs = 100, sleep = realSleep, shouldRetry = () => true } = {}) {
  let delay = baseDelayMs;
  let lastError;
  for (let attempt = 0; attempt <= maxAttempts; attempt++) {
    try {
      return fn(attempt);
    } catch (err) {
      lastError = err;
      if (!shouldRetry) {
        throw err;
      }
      sleep(delay);
      delay += delay;
    }
  }
  throw new Error('retries exhausted');
}

module.exports = { withRetry };
