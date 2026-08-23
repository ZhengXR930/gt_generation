function assert(actual, expected, message) {}

var a, i, n, tab, o, v, n2;
a = new WeakMap();
n = 10;
tab = [];
for (i = 0; i < n; i++) {
    v = {};
    o = {id: i};
    tab[i] = [o, v];
    a.set(o, v);
}
o = null;

n2 = n >> 1;
for (i = 0; i < n2; i++) {
    a.delete(tab[i][0]);
}
for (i = n2; i < n; i++) {
    tab[i][0] = null; /* should remove the object from the WeakMap too */
}
/* the WeakMap should be empty here */
