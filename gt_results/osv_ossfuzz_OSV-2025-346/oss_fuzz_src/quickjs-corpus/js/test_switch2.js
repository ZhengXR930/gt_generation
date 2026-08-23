function assert(actual, expected, message) {}

var i, a, s;
s = "";
for (i = 0; i < 4; i++) {
    a = "?";
    switch (i) {
        case 0:
            a = "a";
            break;
        case 1:
            a = "b";
            break;
        case 2:
            continue;
        default:
            a = "" + i;
            break;
    }
    s += a;
}
assert(s === "ab3" && i === 4);
