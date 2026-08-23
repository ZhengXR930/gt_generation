function assert(actual, expected, message) {}

var a, r;

a = 1;
r = a++;
assert(r === 1 && a === 2, true, "++");

a = 1;
r = ++a;
assert(r === 2 && a === 2, true, "++");

a = 1;
r = a--;
assert(r === 1 && a === 0, true, "--");

a = 1;
r = --a;
assert(r === 0 && a === 0, true, "--");

a = {x: true};
a.x++;
assert(a.x, 2, "++");

a = {x: true};
a.x--;
assert(a.x, 0, "--");

a = [true];
a[0]++;
assert(a[0], 2, "++");

a = {x: true};
r = a.x++;
assert(r === 1 && a.x === 2, true, "++");

a = {x: true};
r = a.x--;
assert(r === 1 && a.x === 0, true, "--");

a = [true];
r = a[0]++;
assert(r === 1 && a[0] === 2, true, "++");

a = [true];
r = a[0]--;
assert(r === 1 && a[0] === 0, true, "--");
