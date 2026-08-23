function assert(actual, expected, message) {}

var i, tab, a, b;

tab = [];
for (i in {x: 1, y: 2}) {
    tab.push(i);
}
assert(tab.toString(), "x,y", "for_in");

/* prototype chain test */
a = {x: 2, y: 2, "1": 3};
b = {"4": 3};
Object.setPrototypeOf(a, b);
tab = [];
for (i in a) {
    tab.push(i);
}
assert(tab.toString(), "1,x,y,4", "for_in");

/* non enumerable properties hide enumerables ones in the
   prototype chain */
a = {y: 2, "1": 3};
Object.defineProperty(a, "x", {value: 1});
b = {"x": 3};
Object.setPrototypeOf(a, b);
tab = [];
for (i in a) {
    tab.push(i);
}
assert(tab.toString(), "1,y", "for_in");

/* array optimization */
a = [];
for (i = 0; i < 10; i++)
    a.push(i);
tab = [];
for (i in a) {
    tab.push(i);
}
assert(tab.toString(), "0,1,2,3,4,5,6,7,8,9", "for_in");

/* iterate with a field */
a = {x: 0};
tab = [];
for (a.x in {x: 1, y: 2}) {
    tab.push(a.x);
}
assert(tab.toString(), "x,y", "for_in");

/* iterate with a variable field */
a = [0];
tab = [];
for (a[0] in {x: 1, y: 2}) {
    tab.push(a[0]);
}
assert(tab.toString(), "x,y", "for_in");

/* variable definition in the for in */
tab = [];
for (var j in {x: 1, y: 2}) {
    tab.push(j);
}
assert(tab.toString(), "x,y", "for_in");

/* variable assigment in the for in */
tab = [];
for (var k = 2 in {x: 1, y: 2}) {
    tab.push(k);
}
assert(tab.toString(), "x,y", "for_in");
