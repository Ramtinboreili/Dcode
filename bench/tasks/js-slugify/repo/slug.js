'use strict';

function slugify(title) {
  return title
    .toLowerCase()
    .replace(' ', '-')
    .replace(/[^a-z0-9-]/, '');
}

module.exports = { slugify };
