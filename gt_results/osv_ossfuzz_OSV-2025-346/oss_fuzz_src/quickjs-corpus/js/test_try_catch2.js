function assert(actual, expected, message) {}

var a;
try {
    a = 1;
} catch (e) {
    a = 2;
}
assert(a, 1, "catch");
