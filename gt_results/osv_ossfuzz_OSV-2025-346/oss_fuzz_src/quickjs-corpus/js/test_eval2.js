function assert(actual, expected, message) {}

var g_call_count = 0;
/* force non strict mode for f1 and f2 */
var f1 = new Function("eval", "eval(1, 2)");
var f2 = new Function("eval", "eval(...[1, 2])");

function g(a, b) {
    assert(a, 1);
    assert(b, 2);
    g_call_count++;
}

f1(g);
f2(g);
assert(g_call_count, 2);
