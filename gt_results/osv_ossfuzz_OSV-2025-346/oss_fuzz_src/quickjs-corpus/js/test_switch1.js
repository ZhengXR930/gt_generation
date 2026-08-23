function assert(actual, expected, message) {}

var i, a, s;
s = "";
for (i = 0; i < 3; i++) {
    a = "?";
    switch (i) {
        case 0:
            a = "a";
            break;
        case 1:
            a = "b";
            break;
        default:
            a = "c";
            break;
    }
    s += a;
}
assert(s === "abc" && i === 3);
