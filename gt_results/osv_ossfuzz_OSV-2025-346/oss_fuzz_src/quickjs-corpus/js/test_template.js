function assert(actual, expected, message) {}

var a, b;
b = 123;
a = `abc${b}d`;
assert(a, "abc123d");

a = String.raw`abc${b}d`;
assert(a, "abc123d");

a = "aaa";
b = "bbb";
assert(`aaa${a, b}ccc`, "aaabbbccc");
