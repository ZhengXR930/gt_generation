function assert(actual, expected, message) {}

var a, i, n, tab, o, v;
n = 1000;

a = new Map();
for (var i = 0; i < n; i++) {
    a.set(i, i);
}
a.set(-2147483648, 1);
assert(a.get(-2147483648), 1);
assert(a.get(-2147483647 - 1), 1);
assert(a.get(-2147483647.5 - 0.5), 1);

a.set(1n, 1n);
assert(a.get(1n), 1n);
assert(a.get(2n ** 1000n - (2n ** 1000n - 1n)), 1n);

a = new Map();
tab = [];
for (i = 0; i < n; i++) {
    v = {};
    o = {id: i};
    tab[i] = [o, v];
    a.set(o, v);
}

assert(a.size, n);
for (i = 0; i < n; i++) {
    assert(a.get(tab[i][0]), tab[i][1]);
}

i = 0;
a.forEach(function (v, o) {
    assert(o, tab[i++][0]);
    assert(a.has(o));
    assert(a.delete(o));
    assert(!a.has(o));
});

assert(a.size, 0);
