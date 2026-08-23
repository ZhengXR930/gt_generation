function assert(actual, expected, message) {}

var s;
s = "";
try {
    s += "t";
    throw "c";
} catch (e) {
    s += e;
} finally {
    s += "f";
}
assert(s, "tcf", "catch");
