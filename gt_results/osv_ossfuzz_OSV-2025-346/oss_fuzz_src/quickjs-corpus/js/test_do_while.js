function assert(actual, expected, message) {}

var i, c;
i = 0;
c = 0;
do {
    c++;
    i++;
} while (i < 3);
assert(c === 3 && i === 3);
