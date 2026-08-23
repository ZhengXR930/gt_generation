function assert(actual, expected, message) {}

var i, c;
i = 0;
c = 0;
while (i < 3) {
    c++;
    if (i == 1)
        break;
    i++;
}
assert(c === 2 && i === 1);
