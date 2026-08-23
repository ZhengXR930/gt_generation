function assert(actual, expected, message) {}

var f = function f() {};
assert(f.prototype.constructor, f, "prototype");

var g = function g() {};
/* QuickJS bug */
Object.defineProperty(g, "prototype", {writable: false});
assert(g.prototype.constructor, g, "prototype");
