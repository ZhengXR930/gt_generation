function assert(actual, expected, message) {}

var i, c;
c = 0;
for (i = 0; i < 3; i++) {
    c++;
}
assert(c === 3 && i === 3);

c = 0;
for (var j = 0; j < 3; j++) {
    c++;
}
assert(c === 3 && j === 3);
