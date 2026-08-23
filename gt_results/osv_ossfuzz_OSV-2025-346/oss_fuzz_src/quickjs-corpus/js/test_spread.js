function assert(actual, expected, message) {}

var x;
x = [1, 2, ...[3, 4]];
assert(x.toString(), "1,2,3,4");

x = [...[,]];
assert(Object.getOwnPropertyNames(x).toString(), "0,length");
