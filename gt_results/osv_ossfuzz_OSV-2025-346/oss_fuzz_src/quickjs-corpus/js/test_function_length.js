function assert(actual, expected, message) {}

assert(((a, b = 1, c) => {}).length, 1);
assert((([a, b]) => {}).length, 1);
assert((({a, b}) => {}).length, 1);
assert(((c, [a, b] = 1, d) => {}).length, 1);
