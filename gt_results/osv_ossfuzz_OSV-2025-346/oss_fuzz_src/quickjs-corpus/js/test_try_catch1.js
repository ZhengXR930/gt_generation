function assert(actual, expected, message) {}

try {
    throw "hello";
} catch (e) {
    assert(e, "hello", "catch");
}
assert(false, "catch");
