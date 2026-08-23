function assert(actual, expected, message) {}

var a = "Bar";
var {b = `${a + `a${a}`}baz`} = {};
assert(b, "BaraBarbaz");
