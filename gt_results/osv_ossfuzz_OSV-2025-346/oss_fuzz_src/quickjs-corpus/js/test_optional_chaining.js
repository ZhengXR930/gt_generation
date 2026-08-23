/* optional chaining tests not present in test262 */

function assert(actual, expected, message) {}

var a, z;
z = null;
a = {b: {c: 2}};
assert(delete z?.b.c, true);
assert(delete a?.b.c, true);
assert(JSON.stringify(a), '{"b":{}}', "optional chaining delete");

a = {b: {c: 2}};
assert(delete z?.b["c"], true);
assert(delete a?.b["c"], true);
assert(JSON.stringify(a), '{"b":{}}');

a = {
    b() {
        return this._b;
    },
    _b: {c: 42}
};

assert((a?.b)().c, 42);
assert((a?.["b"])().c, 42);
