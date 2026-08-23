function assert(actual, expected, message) {}

var s;
s = "";

try {
    try {
        s += "t";
        throw "a";
    } finally {
        s += "f";
    }
} catch (e) {
    s += e;
} finally {
    s += "g";
}
assert(s, "tfag", "catch");
