/* QuickJS BigInt extensions */
function assert(actual, expected, message) {
}

function test_divrem(div1, a, b, q)
{
    var div, divrem, t;
    div = BigInt[div1];
    divrem = BigInt[div1 + "rem"];
    assert(div(a, b) == q);
    t = divrem(a, b);
    assert(t[0] == q);
    assert(a == b * q + t[1]);
}

function test_idiv1(div, a, b, r)
{
    test_divrem(div, a, b, r[0]);
    test_divrem(div, -a, b, r[1]);
    test_divrem(div, a, -b, r[2]);
    test_divrem(div, -a, -b, r[3]);
}

var r;
assert(BigInt.floorLog2(0n) === -1n);
assert(BigInt.floorLog2(7n) === 2n);

assert(BigInt.sqrt(0xffffffc000000000000000n) === 17592185913343n);
r = BigInt.sqrtrem(0xffffffc000000000000000n);
assert(r[0] === 17592185913343n);
assert(r[1] === 35167191957503n);

test_idiv1("tdiv", 3n, 2n, [1n, -1n, -1n, 1n]);
test_idiv1("fdiv", 3n, 2n, [1n, -2n, -2n, 1n]);
test_idiv1("cdiv", 3n, 2n, [2n, -1n, -1n, 2n]);
test_idiv1("ediv", 3n, 2n, [1n, -2n, -1n, 2n]);
