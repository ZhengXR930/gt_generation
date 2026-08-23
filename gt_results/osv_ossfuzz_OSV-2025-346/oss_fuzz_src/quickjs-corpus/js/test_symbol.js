function assert(actual, expected, message) {}

var a, b, obj, c;
a = Symbol("abc");
obj = {};
obj[a] = 2;
assert(obj[a], 2);
assert(typeof obj["abc"], "undefined");
assert(String(a), "Symbol(abc)");
b = Symbol("abc");
assert(a == a);
assert(a === a);
assert(a != b);
assert(a !== b);

b = Symbol.for("abc");
c = Symbol.for("abc");
assert(b === c);
assert(b !== a);

assert(Symbol.keyFor(b), "abc");
assert(Symbol.keyFor(a), undefined);

a = Symbol("aaa");
assert(a.valueOf(), a);
assert(a.toString(), "Symbol(aaa)");

b = Object(a);
assert(b.valueOf(), a);
assert(b.toString(), "Symbol(aaa)");
