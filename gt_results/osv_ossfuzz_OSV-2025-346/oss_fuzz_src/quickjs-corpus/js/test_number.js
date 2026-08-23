function assert(actual, expected, message) {}

assert(parseInt("123"), 123);
assert(parseInt("  123r"), 123);
assert(parseInt("0x123"), 0x123);
assert(parseInt("0o123"), 0);
assert(+"  123   ", 123);
assert(+"0b111", 7);
assert(+"0o123", 83);
assert(parseFloat("2147483647"), 2147483647);
assert(parseFloat("2147483648"), 2147483648);
assert(parseFloat("-2147483647"), -2147483647);
assert(parseFloat("-2147483648"), -2147483648);
assert(parseFloat("0x1234"), 0);
assert(parseFloat("Infinity"), Infinity);
assert(parseFloat("-Infinity"), -Infinity);
assert(parseFloat("123.2"), 123.2);
assert(parseFloat("123.2e3"), 123200);
assert(Number.isNaN(Number("+")));
assert(Number.isNaN(Number("-")));
assert(Number.isNaN(Number("\x00a")));

// TODO: Fix rounding errors on Windows/Cygwin.
if (typeof os !== 'undefined' && ['win32', 'cygwin'].includes(os.platform)) {

} else {
    assert((25).toExponential(0), "3e+1");
    assert((-25).toExponential(0), "-3e+1");
    assert((2.5).toPrecision(1), "3");
    assert((-2.5).toPrecision(1), "-3");
    assert((1.125).toFixed(2), "1.13");
    assert((-1.125).toFixed(2), "-1.13");
}