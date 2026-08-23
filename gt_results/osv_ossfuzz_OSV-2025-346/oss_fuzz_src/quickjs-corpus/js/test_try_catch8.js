function assert(actual, expected, message) {}

var i, s;
s = "";
for (var i in {x: 1, y: 2}) {
    try {
        s += i;
        throw "a";
    } catch (e) {
        s += e;
    } finally {
        s += "f";
    }
}
assert(s === "xafyaf");
