function assert(actual, expected, message) {}
function assertThrows(err, func) {}

function test_less(a, b) {
    assert(a < b);
    assert(!(b < a));
    assert(a <= b);
    assert(!(b <= a));
    assert(b > a);
    assert(!(a > b));
    assert(b >= a);
    assert(!(a >= b));
    assert(a != b);
    assert(!(a == b));
}

/* a must be numerically equal to b */
function test_eq(a, b) {
    assert(a == b);
    assert(b == a);
    assert(!(a != b));
    assert(!(b != a));
    assert(a <= b);
    assert(b <= a);
    assert(!(a < b));
    assert(a >= b);
    assert(b >= a);
    assert(!(a > b));
}

assert(1m === 1m)
assert(1m !== 2m)
test_less(1m, 2m)
test_eq(2m, 2m)

test_less(1, 2m)
test_eq(2, 2m)

test_less(1.1, 2m)
test_eq(Math.sqrt(4), 2m)

test_less(2n, 3m)
test_eq(3n, 3m)

assert(BigDecimal("1234.1") === 1234.1m)
assert(BigDecimal("    1234.1") === 1234.1m)
assert(BigDecimal("    1234.1  ") === 1234.1m)

assert(BigDecimal(0.1) === 0.1m)
assert(BigDecimal(123) === 123m)
assert(BigDecimal(true) === 1m)

assert(123m + 1m === 124m)
assert(123m - 1m === 122m)

assert(3.2m * 3m === 9.6m)
assert(10m / 2m === 5m)
assertThrows(RangeError, () => { 10m / 3m } );

assert(10m % 3m === 1m)
assert(-10m % 3m === -1m)

assert(1234.5m ** 3m === 1881365963.625m)
assertThrows(RangeError, () => { 2m ** 3.1m } );
assertThrows(RangeError, () => { 2m ** -3m } );

assert(BigDecimal.sqrt(2m,
                   { roundingMode: "half-even",
                     maximumSignificantDigits: 4 }) === 1.414m)
assert(BigDecimal.sqrt(101m,
                   { roundingMode: "half-even",
                     maximumFractionDigits: 3 }) === 10.050m)
assert(BigDecimal.sqrt(0.002m,
                   { roundingMode: "half-even",
                     maximumFractionDigits: 3 }) === 0.045m)

assert(BigDecimal.round(3.14159m,
                   { roundingMode: "half-even",
                     maximumFractionDigits: 3 }) === 3.142m)

assert(BigDecimal.add(3.14159m, 0.31212m,
                      { roundingMode: "half-even",
                        maximumFractionDigits: 2 }) === 3.45m)
assert(BigDecimal.sub(3.14159m, 0.31212m,
                      { roundingMode: "down",
                        maximumFractionDigits: 2 }) === 2.82m)
assert(BigDecimal.mul(3.14159m, 0.31212m,
                      { roundingMode: "half-even",
                        maximumFractionDigits: 3 }) === 0.981m)
assert(BigDecimal.mod(3.14159m, 0.31211m,
                      { roundingMode: "half-even",
                        maximumFractionDigits: 4 }) === 0.0205m)
assert(BigDecimal.div(20m, 3m,
                   { roundingMode: "half-even",
                     maximumSignificantDigits: 3 }) === 6.67m)
assert(BigDecimal.div(20m, 3m,
                   { roundingMode: "half-even",
                     maximumFractionDigits: 50 }) ===
       6.66666666666666666666666666666666666666666666666667m)

/* string conversion */
assert((1234.125m).toString(), "1234.125")
assert((1234.125m).toFixed(2), "1234.13")
assert((1234.125m).toFixed(2, "down"), "1234.12")
assert((1234.125m).toExponential(), "1.234125e+3")
assert((1234.125m).toExponential(5), "1.23413e+3")
assert((1234.125m).toExponential(5, "down"), "1.23412e+3")
assert((1234.125m).toPrecision(6), "1234.13")
assert((1234.125m).toPrecision(6, "down"), "1234.12")
assert((-1234.125m).toPrecision(6, "floor"), "-1234.13")
