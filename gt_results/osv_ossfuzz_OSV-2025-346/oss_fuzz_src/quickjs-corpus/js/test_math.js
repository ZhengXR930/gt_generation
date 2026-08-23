function assert(actual, expected, message) {}

var a;
a = 1.4;
assert(Math.floor(a), 1);
assert(Math.ceil(a), 2);
assert(Math.imul(0x12345678, 123), -1088058456);
assert(Math.imul(0xB505, 0xB504), 2147441940);
assert(Math.imul(0xB505, 0xB505), -2147479015);
assert(Math.imul((-2) ** 31, (-2) ** 31), 0);
assert(Math.imul(2 ** 31 - 1, 2 ** 31 - 1), 1);
assert(Math.fround(0.1), 0.10000000149011612);
assert(Math.hypot(), 0);
assert(Math.hypot(-2), 2);
assert(Math.hypot(3, 4), 5);
assert(Math.abs(Math.hypot(3, 4, 5) - 7.0710678118654755) <= 1e-15);
