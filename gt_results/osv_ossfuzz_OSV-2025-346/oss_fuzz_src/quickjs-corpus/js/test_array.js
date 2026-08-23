function assert(actual, expected, message) {}

var a, err;

a = [1, 2, 3];
assert(a.length, 3, "array");
assert(a[2], 3, "array1");

a = new Array(10);
assert(a.length, 10, "array2");

a = new Array(1, 2);
assert(a.length === 2 && a[0] === 1 && a[1] === 2, true, "array3");

a = [1, 2, 3];
a.length = 2;
assert(a.length === 2 && a[0] === 1 && a[1] === 2, true, "array4");

a = [];
a[1] = 10;
a[4] = 3;
assert(a.length, 5);

a = [1, 2];
a.length = 5;
a[4] = 1;
a.length = 4;
assert(a[4] !== 1, true, "array5");

a = [1, 2];
a.push(3, 4);
assert(a.join(), "1,2,3,4", "join");

a = [1, 2, 3, 4, 5];
Object.defineProperty(a, "3", {configurable: false});
err = false;
try {
    a.length = 2;
} catch (e) {
    err = true;
}
assert(err && a.toString() === "1,2,3,4");
