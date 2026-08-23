function assert(actual, expected, message) {}

var i, c;
c = 0;
L1: for (i = 0; i < 3; i++) {
    c++;
    if (i == 0)
        continue;
    while (1) {
        break L1;
    }
}
assert(c === 2 && i === 1);
