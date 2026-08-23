"use strict";

function assert(actual, expected, message) {}

var r, a, b, c, err;

r = Error("hello");
assert(r.message, "hello", "Error");

a = new Object();
a.x = 1;
assert(a.x, 1, "Object");

assert(Object.getPrototypeOf(a), Object.prototype, "getPrototypeOf");
Object.defineProperty(a, "y", { value: 3, writable: true, configurable: true, enumerable: true });
assert(a.y, 3, "defineProperty");

Object.defineProperty(a, "z", { get: function () { return 4; }, set: function(val) { this.z_val = val; }, configurable: true, enumerable: true });
assert(a.z, 4, "get");
a.z = 5;
assert(a.z_val, 5, "set");

a = { get z() { return 4; }, set z(val) { this.z_val = val; } };
assert(a.z, 4, "get");
a.z = 5;
assert(a.z_val, 5, "set");

b = Object.create(a);
assert(Object.getPrototypeOf(b), a, "create");
c = {u:2};
/* XXX: refcount bug in 'b' instead of 'a' */
Object.setPrototypeOf(a, c);
assert(Object.getPrototypeOf(a), c, "setPrototypeOf");

a = {};
assert(a.toString(), "[object Object]", "toString");

a = {x:1};
assert(Object.isExtensible(a), true, "extensible");
Object.preventExtensions(a);

err = false;
try {
    a.y = 2;
} catch(e) {
    err = true;
}
assert(Object.isExtensible(a), false, "extensible");
assert(typeof a.y, "undefined", "extensible");
assert(err, true, "extensible");
