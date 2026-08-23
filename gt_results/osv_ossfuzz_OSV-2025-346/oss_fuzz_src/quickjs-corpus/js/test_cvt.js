function assert(actual, expected, message) {}

assert((NaN | 0) === 0);
assert((Infinity | 0) === 0);
assert(((-Infinity) | 0) === 0);
assert(("12345" | 0) === 12345);
assert(("0x12345" | 0) === 0x12345);
assert(((4294967296 * 3 - 4) | 0) === -4);

assert(("12345" >>> 0) === 12345);
assert(("0x12345" >>> 0) === 0x12345);
assert((NaN >>> 0) === 0);
assert((Infinity >>> 0) === 0);
assert(((-Infinity) >>> 0) === 0);
assert(((4294967296 * 3 - 4) >>> 0) === (4294967296 - 4));
assert((19686109595169230000).toString() === "19686109595169230000");
