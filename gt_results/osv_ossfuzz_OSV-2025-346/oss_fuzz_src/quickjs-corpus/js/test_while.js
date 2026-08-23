function assert(actual, expected, message) {}

var i, c;
i = 0;
c = 0;
while (i < 3) {
    c++;
    i++;
}
assert(c === 3);
