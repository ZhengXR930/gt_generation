function assert(actual, expected, message) {}

assert(std.sprintf("a=%d s=%s", 123, "abc"), "a=123 s=abc");
assert(std.sprintf("%010d", 123), "0000000123");
assert(std.sprintf("%x", -2), "fffffffe");
assert(std.sprintf("%lx", -2), "fffffffffffffffe");
assert(std.sprintf("%10.1f", 2.1), "       2.1");
assert(std.sprintf("%*.*f", 10, 2, -2.13), "     -2.13");
assert(std.sprintf("%#lx", 0x7fffffffffffffffn), "0x7fffffffffffffff");
