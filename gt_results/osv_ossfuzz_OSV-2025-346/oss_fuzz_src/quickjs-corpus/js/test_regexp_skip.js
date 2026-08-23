function assert(actual, expected, message) {}

var a, b;
[a, b = /abc\(/] = [1];
assert(a === 1);

[a, b = /abc\(/] = [2];
assert(a === 2);
