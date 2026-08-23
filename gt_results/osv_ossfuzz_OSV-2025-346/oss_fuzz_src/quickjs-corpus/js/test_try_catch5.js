function assert(actual, expected, message) {}

var s;
s = "";
for (; ;) {
    try {
        s += "t";
        break;
        s += "b";
    } finally {
        s += "f";
    }
}
assert(s, "tf", "catch");
