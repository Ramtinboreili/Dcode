'use strict';

function mapLimit(items, limit, fn) {
  return new Promise((resolve) => {
    const results = [];
    let done = 0;
    items.forEach((item, index) => {
      fn(item, index).then((value) => {
        results.push(value);
        done += 1;
        if (done === items.length) resolve(results);
      });
    });
  });
}

module.exports = { mapLimit };
