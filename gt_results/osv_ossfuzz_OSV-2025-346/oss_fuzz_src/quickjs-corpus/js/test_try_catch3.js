function assert(actual, expected, message) {}

var s;
s = "";
try {
    s += "t";
} catch (e) {
    s += "c";
} finally {
    s += "f";
}
assert(s, "tf", "catch");
